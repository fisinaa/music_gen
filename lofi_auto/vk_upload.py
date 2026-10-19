"""Durable VK video queue. Upload bodies stream from disk; uncertain writes never retry."""
import fcntl
from yandex_archive import with_local_video
import http.client
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlencode, urlsplit
import uuid
from youtube_upload import Uploader as Base, read_json, save_json, digest

class VKError(ValueError):
    pass


def upload_address(url):
    u = urlsplit(url)
    host = u.hostname or ''
    if (u.scheme != 'https' or u.port not in (None, 443) or u.username or u.password or u.fragment
            or not any(host == d or host.endswith('.' + d) for d in ('vk.com', 'vk.ru', 'vkvideo.ru', 'vkuser.net', 'userapi.com'))):
        raise VKError('VK вернул недопустимый адрес загрузки')
    return u


def response_json(response):
    raw = response.read(2 * 1024 * 1024)
    if response.status != 200:
        raise VKError(f'VK HTTP {response.status}. Проверь подключение и доступ.')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise VKError('Некорректный ответ VK') from None
    if value.get('error'):
        error = value['error']
        code = error.get('error_code') if isinstance(error, dict) else None
        code = code if isinstance(code, int) else 'unknown'
        raise VKError(f'Ошибка VK API {code}. Проверь срок токена и права video / доступ к сообществу.')
    return value


def api(token, method, **params):
    conn = http.client.HTTPSConnection('api.vk.com', timeout=60)
    try:
        body = urlencode({**params, 'access_token': token, 'v': '5.199'})
        conn.request('POST', '/method/' + method, body, {'Content-Type': 'application/x-www-form-urlencoded'})
        result = response_json(conn.getresponse())
        if 'response' not in result:
            raise VKError('Нет результата VK API')
        return result['response']
    except (OSError, http.client.HTTPException):
        raise VKError('Соединение с VK прервано') from None
    finally:
        conn.close()


def upload(url, path, progress, stop):
    u = upload_address(url)
    boundary = 'lofi' + uuid.uuid4().hex
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="video_file"; filename="episode.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode()
    tail = f'\r\n--{boundary}--\r\n'.encode()
    conn = http.client.HTTPSConnection(u.hostname, timeout=120)
    try:
        conn.putrequest('POST', u.path + ('?' + u.query if u.query else ''))
        conn.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
        conn.putheader('Content-Length', str(len(head) + path.stat().st_size + len(tail)))
        conn.endheaders()
        conn.send(head)
        sent = 0
        with path.open('rb') as stream:
            while True:
                if stop.is_set():
                    raise VKError('Загрузка прервана остановкой панели; проверь видео в VK')
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                progress(sent)
        conn.send(tail)
        return response_json(conn.getresponse())
    except (OSError, http.client.HTTPException):
        raise VKError('Соединение прервано; результат загрузки нужно проверить в VK') from None
    finally:
        conn.close()


class Credentials:
    def __init__(self, folder):
        self.folder = Path(folder)

    def data(self):
        return read_json(self.folder / 'token.json', {})

    def status(self):
        d = self.data()
        return {'connected': bool(d.get('access_token') and d.get('target')), 'target': d.get('target')}

    def token(self, target_id):
        d = self.data()
        if not d.get('access_token') or d.get('target', {}).get('id') != target_id:
            raise VKError('Подключи исходную страницу VK: bash ~/lofi_auto/connect_vk.sh')
        return d['access_token']


class Uploader:
    db = Base.db
    episode = Base.episode
    get = Base.get
    update = Base.update

    def __init__(self, root, data, episodes, stop=None, credentials=None):
        self.root, self.episodes = Path(root), Path(episodes)
        self.folder = Path(data) / 'vk'
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.folder, 0o700)
        self.stop = stop or threading.Event()
        self.credentials = credentials or Credentials(self.folder)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('''CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY, episode TEXT, target_id INTEGER, video_sha TEXT,
                size INTEGER, payload TEXT, status TEXT, video_id INTEGER, owner_id INTEGER,
                offset INTEGER DEFAULT 0, error TEXT, created REAL, updated REAL, repeat_of TEXT UNIQUE)''')
        os.chmod(self.folder / 'uploads.sqlite', 0o600)

    @with_local_video
    def enqueue(self, name, target_id, repeat_of=None):
        if type(target_id) is not int or target_id == 0:
            raise VKError('Неверная страница VK')
        self.credentials.token(target_id)
        episode = self.episode(name)
        video = (episode / 'episode.mp4').resolve()
        if read_json(episode / 'status.json', {}).get('status') != 'done':
            raise VKError('Выпуск ещё не готов')
        if not video.is_relative_to(episode) or not video.is_file() or not video.stat().st_size:
            raise VKError('Нужен готовый MP4')
        from episode_package import prepare
        package = prepare(self.root, episode)
        before = video.stat()
        sha = digest(video)
        if (video.stat().st_size, video.stat().st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise VKError('MP4 изменился во время проверки')
        # Public video library upload; no wall post or messages are sent.
        payload = {'name': package['title'], 'description': package['description'], 'wallpost': 0,
                   'privacy_view': 'all', 'privacy_comment': 'all'}
        if target_id < 0:
            payload['group_id'] = -target_id
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if repeat_of is not None:
                parent = c.execute('SELECT * FROM uploads WHERE id=?', (repeat_of,)).fetchone()
                if not parent or (parent['target_id'], parent['episode'], parent['video_sha']) != (target_id, name, sha):
                    raise VKError('Повтор не соответствует исходному выпуску и странице')
                child = c.execute('SELECT id FROM uploads WHERE repeat_of=?', (repeat_of,)).fetchone()
                if child:
                    return child['id']
                if parent['status'] not in ('done', 'failed', 'needs_review', 'cancelled'):
                    raise VKError('Дождись завершения загрузки')
                active = c.execute("SELECT id FROM uploads WHERE target_id=? AND video_sha=? AND status IN ('queued','checking','initiating','uploading')", (target_id, sha)).fetchone()
                if active:
                    raise VKError('Выпуск уже загружается')
            else:
                old = c.execute('SELECT id FROM uploads WHERE target_id=? AND video_sha=? ORDER BY created DESC LIMIT 1', (target_id, sha)).fetchone()
                if old:
                    return old['id']
            ident = uuid.uuid4().hex
            c.execute('INSERT INTO uploads(id,episode,target_id,video_sha,size,payload,status,created,updated,repeat_of) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident, name, target_id, sha, before.st_size, json.dumps(payload), 'queued', time.time(), time.time(), repeat_of))
            return ident

    def reupload(self, ident, target_id):
        return self.enqueue(self.get(ident)['episode'], target_id, repeat_of=ident)

    def public(self):
        with self.db() as c:
            rows = c.execute('SELECT id,episode,target_id,size,offset,status,video_id,owner_id,error,created,repeat_of,EXISTS(SELECT 1 FROM uploads child WHERE child.repeat_of=uploads.id) AS has_repeat FROM uploads ORDER BY created DESC LIMIT 100').fetchall()
        return {'account': self.credentials.status(), 'uploads': [dict(r) for r in rows]}

    def action(self, ident, action):
        with self.db() as c:
            if action != 'cancel' or not c.execute("UPDATE uploads SET status='cancelled',updated=? WHERE id=? AND status='queued'", (time.time(), ident)).rowcount:
                raise VKError('Отменить можно только ожидающую загрузку')

    def run_one(self, ident):
        try:
            row = self.get(ident)
            token = self.credentials.token(row['target_id'])
            video = (self.episode(row['episode']) / 'episode.mp4').resolve()
            if not video.is_relative_to(self.episode(row['episode'])) or video.stat().st_size != row['size'] or digest(video) != row['video_sha']:
                raise VKError('MP4 изменился; создай новый выпуск')
            # Record intent before the first remote write, including crashes/lost replies.
            self.update(ident, status='initiating')
            saved = api(token, 'video.save', **json.loads(row['payload']))
            owner, vid = saved.get('owner_id'), saved.get('video_id')
            if type(owner) is not int or owner != row['target_id'] or type(vid) is not int or vid <= 0:
                raise VKError('VK вернул неожиданную страницу или ID видео; проверь VK')
            self.update(ident, owner_id=owner, video_id=vid, status='uploading')
            result = upload(saved['upload_url'], video, lambda n: self.update(ident, offset=n), self.stop)
            if result.get('video_id') != vid or result.get('owner_id') != owner:
                raise VKError('Нет однозначного подтверждения загрузки; проверь VK')
            self.update(ident, status='done', offset=row['size'], error=None)
        except Exception as exc:
            current = self.get(ident)
            status = 'needs_review' if current['status'] in ('initiating', 'uploading') else 'failed'
            message = str(exc) if isinstance(exc, VKError) else 'Ошибка загрузки VK. Проверь файл и состояние видео в VK.'
            self.update(ident, status=status, error=message)

    def recover(self):
        with self.db() as c:
            c.execute("UPDATE uploads SET status='needs_review',error='Панель перезапущена во время загрузки. Проверь видео в VK перед повтором.' WHERE status IN ('initiating','uploading')")
            c.execute("UPDATE uploads SET status='queued' WHERE status='checking'")

    def run(self):
        with open(self.folder / 'worker.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self.recover()
            while not self.stop.is_set():
                with self.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    row = c.execute("SELECT id FROM uploads WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                    if row:
                        c.execute("UPDATE uploads SET status='checking' WHERE id=?", (row['id'],))
                if row:
                    self.run_one(row['id'])
                else:
                    self.stop.wait(1)
