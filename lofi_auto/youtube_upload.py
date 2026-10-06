"""YouTube uploads with selectable visibility with durable resumable sessions. No third-party SDK."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid

API = 'https://www.googleapis.com/youtube/v3/'
UPLOAD = 'https://www.googleapis.com/upload/youtube/v3/'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']
CHUNK = 8 * 1024 * 1024

class UploadError(Exception):
    pass

class Retryable(UploadError):
    pass

class ReviewRequired(UploadError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path, value):
    """Durable replacement; neither tokens nor session URLs enter public files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def google_url(url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in ('www.googleapis.com', 'oauth2.googleapis.com') or u.port not in (None, 443) or u.username or u.password or u.fragment:
        raise UploadError('Недопустимый адрес Google API')
    return url


def http(method, url, headers=None, body=None):
    google_url(url)
    try:
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers or {}, method=method), timeout=60) as response:
            return response.status, dict(response.headers.items()), response.read(2 * 1024 * 1024)
    except HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(2 * 1024 * 1024)
    except (OSError, URLError, TimeoutError):
        # Do not leak bearer tokens, authorization codes or session URLs in errors.
        raise Retryable('Соединение с Google прервано; загрузку можно продолжить') from None


def decoded(body):
    try:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise UploadError('Некорректный ответ Google API') from None


def api_error(code, body):
    if code == 429 or code >= 500:
        raise Retryable(f'Google API HTTP {code}; повторим с задержкой')
    reason = ''
    try:
        error = json.loads(body).get('error', {})
        reason = error if isinstance(error, str) else error.get('errors', [{}])[0].get('reason', '')
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    reason = reason if re.fullmatch(r'[A-Za-z0-9_]{1,80}', reason or '') else 'requestFailed'
    raise UploadError(f'Google API HTTP {code}: {reason}. Проверь доступ, квоту и авторизацию.')


class Credentials:
    def __init__(self, folder, transport=http):
        self.folder = Path(folder)
        self.transport = transport

    def status(self):
        data = read_json(self.folder / 'token.json', {})
        channel = data.get('channel')
        return {'connected': bool(data.get('refresh_token') and channel), 'channel': channel}

    def access_token(self, force=False):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.folder / 'token.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read_json(self.folder / 'token.json', {})
            if getattr(self, 'expected_channel', None) and data.get('channel', {}).get('id') != self.expected_channel:
                raise UploadError('Подключён другой канал. Верни исходный канал и повтори.')
            if not data.get('refresh_token'):
                raise UploadError('Подключи канал командой bash ~/lofi_auto/connect_youtube.sh')
            if not force and data.get('access_token') and data.get('expires_at', 0) > time.time() + 60:
                return data['access_token']
            body = urlencode({'client_id': data['client_id'], 'client_secret': data['client_secret'],
                              'refresh_token': data['refresh_token'], 'grant_type': 'refresh_token'}).encode()
            code, _, payload = self.transport('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, body)
            if code != 200:
                api_error(code, payload)
            token = decoded(payload)
            if not token.get('access_token'):
                raise UploadError('Google не вернул токен доступа; подключи канал заново')
            data.update(access_token=token['access_token'], expires_at=time.time() + int(token.get('expires_in', 3600)))
            if token.get('refresh_token'):
                data['refresh_token'] = token['refresh_token']
            save_json(self.folder / 'token.json', data)
            return data['access_token']

    def request(self, method, url, headers=None, body=None):
        google_url(url)
        for attempt in range(2):
            token = self.access_token(force=bool(attempt))
            code, response_headers, payload = self.transport(method, url, {**(headers or {}), 'Authorization': 'Bearer ' + token}, body)
            if code != 401:
                return code, {k.lower(): v for k, v in response_headers.items()}, payload
        api_error(code, payload)

    def channel(self):
        code, _, payload = self.request('GET', API + 'channels?part=snippet&mine=true')
        if code != 200:
            api_error(code, payload)
        items = decoded(payload).get('items', [])
        if len(items) != 1:
            raise UploadError('Нужна авторизация одного YouTube-канала; выбери нужный канал при подключении')
        return {'id': items[0]['id'], 'title': items[0]['snippet']['title']}


class Uploader:
    def __init__(self, root, data, episodes, stop=None, credentials=None):
        self.root = Path(root)
        self.folder = Path(data) / 'youtube'
        self.episodes = Path(episodes)
        self.stop = stop or threading.Event()
        self.credentials = credentials or Credentials(self.folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.folder, 0o700)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('BEGIN IMMEDIATE')
            columns = [r['name'] for r in c.execute('PRAGMA table_info(uploads)')]
            legacy = bool(columns) and 'repeat_of' not in columns
            if legacy:
                c.execute('ALTER TABLE uploads RENAME TO uploads_legacy')
            c.execute('''CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY, episode TEXT, channel_id TEXT, video_sha TEXT,
                size INTEGER, payload TEXT, status TEXT, session TEXT, video_id TEXT,
                offset INTEGER DEFAULT 0, error TEXT, created REAL, updated REAL,
                repeat_of TEXT UNIQUE)''')
            if legacy:
                names = 'id,episode,channel_id,video_sha,size,payload,status,session,video_id,offset,error,created,updated'
                c.execute(f'INSERT INTO uploads({names}) SELECT {names} FROM uploads_legacy')
                c.execute('DROP TABLE uploads_legacy')
            if 'actual_privacy' not in [r['name'] for r in c.execute('PRAGMA table_info(uploads)')]:
                c.execute('ALTER TABLE uploads ADD COLUMN actual_privacy TEXT')
            c.execute('CREATE INDEX IF NOT EXISTS uploads_content ON uploads(channel_id,video_sha,created)')
        os.chmod(self.folder / 'uploads.sqlite', 0o600)

    def db(self):
        c = sqlite3.connect(self.folder / 'uploads.sqlite', timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def episode(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Неверный выпуск')
        path = (self.episodes / name).resolve()
        if not path.is_relative_to(self.episodes.resolve()) or not path.is_dir():
            raise ValueError('Выпуск не найден')
        return path

    def enqueue(self, name, channel_id, made_for_kids, synthetic, repeat_of=None, privacy="private"):
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError("Выбери доступ: ограниченный, по ссылке или открытый")
        if type(made_for_kids) is not bool or type(synthetic) is not bool:
            raise ValueError('Укажи аудиторию и наличие синтетического контента')
        channel = self.credentials.status().get('channel')
        if not channel or channel['id'] != channel_id:
            raise ValueError('Выбранный канал изменился; обнови страницу')
        episode = self.episode(name)
        if read_json(episode / 'status.json', {}).get('status') != 'done':
            raise ValueError('Выпуск ещё не завершён')
        video = (episode / 'episode.mp4').resolve()
        if not video.is_relative_to(episode) or not video.is_file() or video.stat().st_size == 0:
            raise ValueError('Нужен готовый MP4; выпуск только с музыкой загрузить нельзя')
        from episode_package import prepare
        package = prepare(self.root, episode)
        # Snapshot exactly what the user reviewed; subsequent edits do not alter an upload.
        thumb = (episode / 'thumbnail.jpg').resolve()
        if not thumb.is_relative_to(episode) or thumb.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Нужна локальная JPEG-обложка не больше 2 MB')
        before = video.stat()
        sha = digest(video)
        if (video.stat().st_size, video.stat().st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError('MP4 изменился во время проверки')
        payload = {'snippet': {'title': package['title'], 'description': package['description'],
                              'categoryId': '10', 'defaultLanguage': 'en'},
                   'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': made_for_kids,
                              'containsSyntheticMedia': synthetic}}
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if repeat_of is not None:
                parent = c.execute('SELECT * FROM uploads WHERE id=?', (repeat_of,)).fetchone()
                if not parent or parent['channel_id'] != channel_id or parent['video_sha'] != sha or parent['episode'] != name:
                    raise ValueError('Исходная загрузка не соответствует выпуску или каналу')
                child = c.execute('SELECT id,payload FROM uploads WHERE repeat_of=?', (repeat_of,)).fetchone()
                if child:
                    if json.loads(child['payload'])['status']['privacyStatus'] != privacy:
                        raise ValueError('Повтор уже создан с другим доступом; используй его запись')
                    return child['id']  # Retried HTTP request / double click: same new attempt.
                if parent['status'] not in ('done','failed','thumbnail_failed','needs_review','cancelled'):
                    raise ValueError('Дождись завершения текущей загрузки')
                active = c.execute("SELECT id FROM uploads WHERE channel_id=? AND video_sha=? AND status IN ('queued','initiating','uploading','thumbnail')", (channel_id, sha)).fetchone()
                if active:
                    raise ValueError('Этот выпуск уже стоит в очереди или загружается')
            else:
                old = c.execute('SELECT id,payload FROM uploads WHERE channel_id=? AND video_sha=? ORDER BY created DESC,id DESC LIMIT 1', (channel_id, sha)).fetchone()
                if old:
                    if json.loads(old['payload'])['status']['privacyStatus'] != privacy:
                        raise ValueError('Этот выпуск уже отправлялся с другим доступом. Выбери «Загрузить заново» во вкладке YouTube.')
                    return old['id']
            ident = uuid.uuid4().hex
            tmp = self.folder / (ident + '.jpg.tmp')
            shutil.copyfile(thumb, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.folder / (ident + '.jpg'))
            c.execute('INSERT INTO uploads(id,episode,channel_id,video_sha,size,payload,status,created,updated,repeat_of) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident, name, channel_id, sha, before.st_size, json.dumps(payload), 'queued', time.time(), time.time(), repeat_of))
            return ident

    def reupload(self, ident, channel_id, privacy=None):
        row = self.get(ident)
        if row['channel_id'] != channel_id:
            raise ValueError('Повторная загрузка должна идти на исходный канал')
        status = json.loads(row['payload'])['status']
        return self.enqueue(row['episode'], channel_id, status['selfDeclaredMadeForKids'],
                            status['containsSyntheticMedia'], repeat_of=ident,
                            privacy=status['privacyStatus'] if privacy is None else privacy)

    def public(self):
        with self.db() as c:
            rows = c.execute('SELECT id,episode,channel_id,size,offset,status,video_id,error,created,updated,repeat_of,payload,actual_privacy,EXISTS(SELECT 1 FROM uploads child WHERE child.repeat_of=uploads.id) AS has_repeat FROM uploads ORDER BY created DESC LIMIT 100').fetchall()
        uploads=[]
        for row in rows:
            item=dict(row)
            item['privacy']=json.loads(item.pop('payload'))['status']['privacyStatus']
            uploads.append(item)
        return {'account': self.credentials.status(), 'uploads': uploads}

    def get(self, ident):
        with self.db() as c:
            row = c.execute('SELECT * FROM uploads WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('Загрузка не найдена')
        return dict(row)

    def update(self, ident, **values):
        values['updated'] = time.time()
        with self.db() as c:
            c.execute('UPDATE uploads SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', [*values.values(), ident])

    def action(self, ident, action):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM uploads WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('Загрузка не найдена')
            if action == 'retry' and c.execute('SELECT 1 FROM uploads WHERE repeat_of=?', (ident,)).fetchone():
                raise ValueError('Для этой записи уже создана новая загрузка; используй её')
            if action == 'retry' and row['status'] in ('failed', 'thumbnail_failed', 'cancelled'):
                c.execute("UPDATE uploads SET status='queued',error=NULL,updated=? WHERE id=?", (time.time(), ident))
            elif action == 'cancel' and row['status'] == 'queued' and not row['session'] and not row['video_id']:
                c.execute("UPDATE uploads SET status='cancelled',updated=? WHERE id=?", (time.time(), ident))
            else:
                raise ValueError('Действие недоступно; при неоднозначном результате проверь YouTube Studio')

    def acknowledge(self, row, code, headers, body):
        if code in (200, 201):
            response = decoded(body)
            video_id = response.get('id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                raise ReviewRequired('Ответ об окончании без ID видео. Проверь YouTube Studio; новая загрузка не начата.')
            actual=response.get('status',{}).get('privacyStatus')
            self.update(row['id'], video_id=video_id, offset=row['size'], status='thumbnail', error=None,
                        actual_privacy=actual if actual in ('private','unlisted','public') else None)
            return row['size'], video_id
        if code == 308:
            value = headers.get('range')
            match = re.fullmatch(r'bytes=0-(\d+)', value or '')
            if value and not match:
                raise ReviewRequired('Некорректный диапазон ответа YouTube')
            offset = int(match[1]) + 1 if match else 0
            if offset < 0 or offset > row['size']:
                raise ReviewRequired('YouTube вернул неожиданный размер загрузки')
            self.update(row['id'], offset=offset)
            return offset, None
        if code in (404, 410):
            raise ReviewRequired('Сессия загрузки истекла. Проверь YouTube Studio; автоматическая новая загрузка заблокирована во избежание дубля.')
        api_error(code, body)

    def transfer(self, row):
        ident = row['id']
        self.credentials.expected_channel = row['channel_id']
        if self.credentials.channel()['id'] != row['channel_id']:
            raise UploadError('Подключён другой канал. Верни исходный канал и повтори.')
        if not row['video_id']:
            video = (self.episode(row['episode']) / 'episode.mp4').resolve()
            if not video.is_relative_to(self.episode(row['episode'])) or video.stat().st_size != row['size'] or digest(video) != row['video_sha']:
                raise UploadError('MP4 изменился; продолжать прежнюю загрузку нельзя')
            if not row['session']:
                # Persist intent before calling POST; never silently create a second session.
                self.update(ident, status='initiating')
                payload = json.dumps(json.loads(row['payload'])).encode()
                code, headers, body = self.credentials.request('POST', UPLOAD + 'videos?uploadType=resumable&part=snippet,status&notifySubscribers=false',
                    {'Content-Type': 'application/json; charset=UTF-8', 'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(row['size'])}, payload)
                if code not in (200, 201):
                    # No media was sent; definite HTTP rejection is safe to retry manually.
                    self.update(ident, status='uploading')
                    api_error(code, body)
                session = headers.get('location', '')
                google_url(session)
                if not urlsplit(session).path.startswith('/upload/youtube/v3/videos'):
                    raise ReviewRequired('Неожиданный адрес сессии загрузки')
                self.update(ident, session=session, status='uploading')
                row['session'] = session
            # Query server offset on every resumed attempt, including a lost final response.
            code, headers, body = self.credentials.request('PUT', row['session'], {'Content-Range': f"bytes */{row['size']}", 'Content-Length': '0'}, b'')
            offset, video_id = self.acknowledge(row, code, headers, body)
            with open(video, 'rb') as stream:
                while video_id is None and offset < row['size']:
                    if self.stop.is_set():
                        raise Retryable('Панель остановлена; сессия сохранена')
                    stream.seek(offset)
                    chunk = stream.read(min(CHUNK, row['size'] - offset))
                    if not chunk:
                        raise UploadError('MP4 неожиданно закончился')
                    code, headers, body = self.credentials.request('PUT', row['session'],
                        {'Content-Type': 'video/mp4', 'Content-Range': f"bytes {offset}-{offset + len(chunk) - 1}/{row['size']}"}, chunk)
                    new_offset, video_id = self.acknowledge(row, code, headers, body)
                    if video_id is None and new_offset <= offset:
                        raise Retryable('YouTube пока не подтвердил следующий блок')
                    offset = new_offset
            if video_id is None:
                raise Retryable('Все байты приняты, ожидается подтверждение ID видео')
        # ID is durably stored BEFORE thumbnail upload. A thumbnail failure never reuploads MP4.
        row = self.get(ident)
        self.update(ident, status='thumbnail')
        thumbnail = (self.folder / (ident + '.jpg')).read_bytes()
        code, _, body = self.credentials.request('POST', UPLOAD + 'thumbnails/set?uploadType=media&videoId=' + row['video_id'],
                                                  {'Content-Type': 'image/jpeg'}, thumbnail)
        if code != 200:
            api_error(code, body)
        self.update(ident, status='done', error=None)

    def run_one(self, ident):
        for attempt in range(4):
            row = self.get(ident)
            try:
                self.transfer(row)
                return
            except ReviewRequired as exc:
                self.update(ident, status='needs_review', error=str(exc))
                return
            except Retryable as exc:
                current = self.get(ident)
                if current['status'] == 'initiating' and not current['session']:
                    self.update(ident, status='needs_review', error='Потерян ответ при создании сессии. Проверь YouTube Studio; автоматический повтор остановлен.')
                    return
                if self.stop.is_set():
                    self.update(ident, status='queued', error=None)
                    return
                if attempt < 3:
                    self.update(ident, error=f'{exc} Попытка {attempt + 2}/4.')
                    if self.stop.wait(2 ** attempt):
                        self.update(ident, status='queued', error=None)
                        return
                    continue
                self.update(ident, status='thumbnail_failed' if current['video_id'] else 'failed', error=str(exc))
            except Exception as exc:
                current = self.get(ident)
                # Unexpected errors are redacted; credentials and session URLs stay private.
                message = str(exc) if isinstance(exc, (UploadError, ValueError)) else 'Локальная ошибка загрузки. Проверь наличие файлов и свободное место.'
                status = 'needs_review' if current['status'] == 'initiating' else ('thumbnail_failed' if current['video_id'] else 'failed')
                self.update(ident, status=status, error=message)
                return

    def run(self):
        with open(self.folder / 'worker.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.db() as c:
                c.execute("UPDATE uploads SET status='needs_review',error='Перезапуск при создании сессии: проверь YouTube Studio' WHERE status='initiating' AND session IS NULL")
                c.execute("UPDATE uploads SET status='queued' WHERE status IN ('uploading','thumbnail')")
            while not self.stop.is_set():
                with self.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    row = c.execute("SELECT id FROM uploads WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                    if row:
                        c.execute("UPDATE uploads SET status='uploading' WHERE id=?", (row['id'],))
                if row:
                    self.run_one(row['id'])
                else:
                    self.stop.wait(1)
