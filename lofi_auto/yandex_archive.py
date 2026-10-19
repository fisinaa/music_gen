"""Private final-episode archive; fail-closed cleanup and verified restoration."""
import contextlib
import fcntl
import functools
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from urllib.parse import urlencode, urlsplit
def read_json(*args, **kwargs):
    from youtube_upload import read_json as impl
    return impl(*args, **kwargs)

def save_json(*args, **kwargs):
    from youtube_upload import save_json as impl
    return impl(*args, **kwargs)

def digest(*args, **kwargs):
    from youtube_upload import digest as impl
    return impl(*args, **kwargs)

API = 'https://cloud-api.yandex.net/v1/disk/'
FILES = ('episode.mp4', 'thumbnail.jpg', 'title.txt', 'description.txt', 'publication.json')

class ArchiveError(ValueError):
    pass

@contextlib.contextmanager
def episode_lock(episode):
    with open(Path(episode)/'.archive.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def with_local_video(method):
    """Serialize enqueue+restore against cleanup, including direct uploader calls."""
    @functools.wraps(method)
    def wrapped(self, name, *args, **kwargs):
        episode = self.episode(name)
        with episode_lock(episode):
            if not (episode/'episode.mp4').exists() and (episode/'archive.json').exists():
                Disk(self.folder.parent).restore(episode)
            return method(self, name, *args, **kwargs)
    return wrapped


class Disk:
    def __init__(self, data):
        self.folder = Path(data)/'yandex'

    def config(self):
        return read_json(self.folder/'config.json', {})

    def request(self, method, url, *, auth=False, source=None, destination=None):
        u = urlsplit(url)
        host = u.hostname or ''
        if (u.scheme != 'https' or u.port not in (None,443) or u.username or u.password or u.fragment
                or not any(host == d or host.endswith('.'+d) for d in ('yandex.net','yandex.ru','yandex.com'))
                or (auth and host != 'cloud-api.yandex.net')):
            raise ArchiveError('Недопустимый адрес Яндекс Диска')
        headers = {}
        if auth:
            token = self.config().get('token')
            if not token:raise ArchiveError('Сначала подключи Яндекс Диск')
            headers['Authorization'] = 'OAuth '+token
        conn = http.client.HTTPSConnection(host, timeout=120)
        try:
            with contextlib.ExitStack() as stack:
                body = None
                if source is not None:
                    body = stack.enter_context(open(source, 'rb'))
                    headers['Content-Length'] = str(os.fstat(body.fileno()).st_size)
                    headers['Content-Type'] = 'application/octet-stream'
                conn.request(method, u.path+('?' + u.query if u.query else ''), body, headers)
                response = conn.getresponse()
                if destination is not None and response.status == 200:
                    with open(destination, 'wb') as out:
                        while chunk := response.read(1024*1024):out.write(chunk)
                        out.flush();os.fsync(out.fileno())
                    return response.status, {}
                raw = response.read(2*1024*1024)
                return response.status, json.loads(raw) if raw else {}
        except (OSError, http.client.HTTPException, ValueError):
            raise ArchiveError('Ошибка связи с Яндекс Диском; локальные файлы сохранены') from None
        finally:conn.close()

    def api(self, method, endpoint='', **params):
        return self.request(method, API+endpoint+('?' + urlencode(params) if params else ''), auth=True)

    def meta(self, path):
        code, result = self.api('GET', 'resources', path=path)
        if code == 404:return None
        if code != 200:raise ArchiveError(f'Яндекс Диск HTTP {code}; проверь токен и доступ')
        return result

    def mkdir(self, path):
        code, _ = self.api('PUT', 'resources', path=path)
        if code == 409:
            if (self.meta(path) or {}).get('type') == 'dir':return
        elif code == 201:return
        raise ArchiveError(f'Не удалось создать папку архива: HTTP {code}')

    def verify(self, path, record):
        remote = self.meta(path)
        return bool(remote and remote.get('type') == 'file' and
                    remote.get('size') == record['size'] and remote.get('sha256') == record['sha256'])

    def archive(self, episode):
        state = read_json(episode/'archive.json', {})
        if state.get('cleaned_at'):return state
        # Content-addressed folder prevents overwriting an earlier, different video.
        video = episode/'episode.mp4'
        sha = digest(video)
        remote = 'disk:/Lofi/'+episode.name+'/'+sha
        for path in ('disk:/Lofi', 'disk:/Lofi/'+episode.name, remote):self.mkdir(path)
        records = {}
        for name in FILES:
            local = episode/name
            if local.is_symlink() or not local.is_file():raise ArchiveError('Не хватает файлов готового выпуска')
            record = {'size':local.stat().st_size, 'sha256':digest(local), 'path':remote+'/'+name}
            if not self.verify(record['path'], record):
                # A different existing file is never silently overwritten.
                if self.meta(record['path']) is not None:
                    raise ArchiveError('Файл в архиве отличается; автоматическая перезапись остановлена')
                code, link = self.api('GET', 'resources/upload', path=record['path'], overwrite='false')
                if code != 200:raise ArchiveError(f'Не удалось начать загрузку: HTTP {code}')
                code, _ = self.request('PUT', link['href'], source=local)
                if code not in (201,202):raise ArchiveError(f'Ошибка загрузки: HTTP {code}')
                if not self.verify(record['path'], record):
                    raise ArchiveError('Диск ещё не подтвердил размер и SHA-256; повторим позже')
            if digest(local) != record['sha256']:raise ArchiveError('Локальный файл изменился')
            records[name] = record
        state = {'status':'verified', 'path':remote, 'files':records, 'verified_at':time.time()}
        save_json(episode/'archive.json', state)
        return state

    def restore(self, episode):
        state = read_json(episode/'archive.json', {})
        record = state.get('files', {}).get('episode.mp4')
        if not record or not self.verify(record['path'], record):raise ArchiveError('Архив видео не прошёл проверку')
        if shutil.disk_usage(episode).free < record['size'] + 512*1024**2:
            raise ArchiveError('Недостаточно места для восстановления видео')
        code, link = self.api('GET', 'resources/download', path=record['path'])
        if code != 200:raise ArchiveError('Не удалось получить видео из архива')
        tmp = episode/'episode.restore.partial'
        try:
            code, _ = self.request('GET', link['href'], destination=tmp)
            if code != 200 or tmp.stat().st_size != record['size'] or digest(tmp) != record['sha256']:
                raise ArchiveError('Скачанное видео не прошло проверку')
            os.replace(tmp, episode/'episode.mp4')
            # Keep restored copies briefly so a new enqueue can register its intent.
            state['restored_at'] = time.time()
            save_json(episode/'archive.json', state)
        finally:tmp.unlink(missing_ok=True)


class Archiver:
    def __init__(self, root, data, episodes, uploaders, stop):
        self.root, self.episodes = Path(root), Path(episodes)
        self.disk, self.uploaders, self.stop = Disk(data), uploaders, stop

    def published(self, name, sha):
        found = False
        for uploader in self.uploaders:
            with uploader.db() as db:
                # Superseded failures do not block a successful explicit reupload.
                rows = db.execute('SELECT * FROM uploads WHERE episode=? AND NOT EXISTS '
                    '(SELECT 1 FROM uploads child WHERE child.repeat_of=uploads.id)', (name,)).fetchall()
            for row in rows:
                if row['status'] == 'cancelled':continue
                if row['status'] != 'done' or row['video_sha'] != sha:return False
                found = True
                # Upload acceptance alone is insufficient: wait for platform processing.
                if uploader.folder.name == 'youtube':
                    code, _, raw = uploader.credentials.request('GET',
                        'https://www.googleapis.com/youtube/v3/videos?part=status,processingDetails&id='+row['video_id'])
                    items = json.loads(raw).get('items', []) if code == 200 else []
                    if len(items) != 1 or items[0].get('status',{}).get('uploadStatus') != 'processed':return False
                else:
                    from vk_upload import api
                    result = api(uploader.credentials.token(row['target_id']), 'video.get',
                                 videos=f"{row['owner_id']}_{row['video_id']}")
                    items = result.get('items', [])
                    if len(items) != 1 or items[0].get('processing') or not items[0].get('player'):return False
        return found

    def prune_library(self, episode):
        music = Path(os.environ.get('LOFI_MUSIC_DIR',str(self.root/'music'))).expanduser().resolve()
        queue = self.disk.folder.parent/'queue.sqlite'
        if not music.is_dir() or not queue.is_file():return
        # Web enqueue validates under this same DB transaction. Generation/export
        # is excluded by .pipeline.lock. Shared or favorite tracks are retained.
        with sqlite3.connect(queue, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            protected = {row[0] for row in db.execute('SELECT sha FROM preferences WHERE favorite=1')}
            for payload, in db.execute("SELECT payload FROM jobs WHERE name!=? AND status!='cancelled'", (episode.name,)):
                protected.update(t['sha'] for t in json.loads(payload).get('tracks',[]))
            with open(music/'.catalog.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                catalog = read_json(music/'catalog.json',{})
                references = []
                for other in self.episodes.iterdir():
                    if other == episode or not other.is_dir():continue
                    if read_json(other/'archive.json',{}).get('cleaned_at'):continue
                    for name in ('tracklist.json','settings.json'):
                        path = other/name
                        if path.is_file():references.append(path.read_text())
                for sha, record in list(catalog.items()):
                    sources = record.get('sources',[])
                    if sha in protected or not sources:continue
                    if not all(Path(src).resolve().is_relative_to(episode.resolve()) for src in sources):continue
                    name = record.get('filename','')
                    if not name or Path(name).name != name:continue
                    path = music/name
                    if any(sha in ref or str(path) in ref for ref in references):continue
                    # A build that uses the whole library as its source is protected.
                    if any(str(music) in ref and '"source"' in ref for ref in references):continue
                    if path.is_symlink():continue
                    if path.exists():
                        if digest(path) != sha:continue
                        path.unlink()
                    del catalog[sha]
                save_json(music/'catalog.json',catalog)

    def cleanup(self, episode, state):
        if time.time()-state.get('restored_at',0) < 3600:return
        if not self.published(episode.name, state['files']['episode.mp4']['sha256']):return
        # Serialize against both CLI generation and web generation. Enqueue is already
        # serialized by episode_lock; queued uploads block published() above.
        with open(self.root/'.pipeline.lock', 'a') as lock:
            try:fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            if read_json(episode/'status.json',{}).get('status') != 'done':return
            for other in self.episodes.iterdir():
                if other == episode or not other.is_dir():continue
                if read_json(other/'archive.json',{}).get('cleaned_at'):continue
                for filename in ('settings.json','tracklist.json'):
                    ref = other/filename
                    if ref.is_file() and str(episode.resolve())+'/' in ref.read_text():return
            for record in state['files'].values():
                if not self.disk.verify(record['path'],record):raise ArchiveError('Архив изменился; очистка остановлена')
            video = episode/'episode.mp4'
            if video.exists() and digest(video) != state['files']['episode.mp4']['sha256']:
                raise ArchiveError('Видео изменилось; очистка остановлена')
            # Only fixed episode-local names. External sources/shared library untouched.
            for name in ('work','tracks','selected_tracks','mix.wav','episode.mp4'):
                path = episode/name
                if path.is_symlink():raise ArchiveError('Символическая ссылка: очистка остановлена')
            state.update(status='cleaning', error=None)
            save_json(episode/'archive.json', state)
            self.prune_library(episode)
            for name in ('work','tracks','selected_tracks','mix.wav','episode.mp4'):
                path = episode/name
                if path.is_dir():shutil.rmtree(path)
                else:path.unlink(missing_ok=True)
            state.update(status='archived', cleaned_at=time.time())
            save_json(episode/'archive.json', state)

    def process(self, episode):
        if episode.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_-]+',episode.name):return
        with episode_lock(episode):
            if read_json(episode/'status.json',{}).get('status') != 'done':return
            old = read_json(episode/'archive.json',{})
            if old.get('cleaned_at') and not (episode/'episode.mp4').exists():return
            if not (episode/'episode.mp4').exists() and not old.get('files'):return
            try:
                # Resume interrupted cleanup without requiring files already removed.
                state = old if old.get('files') else self.disk.archive(episode)
                self.cleanup(episode,state)
            except Exception as exc:
                state = read_json(episode/'archive.json',{})
                state['error'] = str(exc) if isinstance(exc,ArchiveError) else 'Архивирование не завершено; повторим позже'
                save_json(episode/'archive.json',state)

    def run(self):
        while not self.stop.is_set():
            if self.disk.config().get('enabled'):
                for episode in sorted(self.episodes.glob('*')):
                    if self.stop.is_set():return
                    if episode.is_dir():
                        try:self.process(episode)
                        except Exception:pass  # A bad folder must not kill the worker.
            self.stop.wait(60)


def connect():
    root = Path(__file__).resolve().parent
    data = Path(os.environ.get('LOFI_WEB_DATA',str(root/'web_data'))).expanduser()
    disk = Disk(data)
    token = getpass.getpass('OAuth-токен Яндекс Диска (ввод скрыт): ').strip()
    if not token:raise SystemExit('Пустой токен')
    old = disk.config()
    save_json(disk.folder/'config.json', {'token':token,'enabled':False})
    try:
        disk.mkdir('disk:/Lofi')
        if (disk.meta('disk:/Lofi') or {}).get('type') != 'dir':
            raise ArchiveError('Не удалось прочитать папку /Lofi; нужны права чтения и записи файлов')
    except Exception as exc:
        save_json(disk.folder/'config.json',old)
        reason = str(exc) if isinstance(exc, ArchiveError) else 'Локальная ошибка подключения'
        raise SystemExit(f'Подключение не удалось: {reason}. Прежняя настройка сохранена') from None
    save_json(disk.folder/'config.json', {'token':token,'enabled':True})
    print('Яндекс Диск подключён: /Lofi. Архивирование и очистка после публикации включены.')

if __name__ == '__main__':connect()
