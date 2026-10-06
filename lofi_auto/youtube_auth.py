#!/usr/bin/env python3
"""One-time Desktop OAuth + PKCE on loopback, usable through an SSH tunnel."""
import argparse
import base64
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from youtube_upload import Credentials, SCOPES, TOKEN_URL, api_error, decoded, http, save_json


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def authorize(folder, client_path, port=8765, transport=http, input_fn=input):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    client = json.loads(Path(client_path).read_text()).get('installed')
    if not isinstance(client, dict) or not client.get('client_id') or not client.get('client_secret'):
        raise ValueError('Нужен JSON OAuth-клиента типа Desktop app, скачанный из Google Cloud')
    with open(folder / 'connect.lock', 'a') as connect_lock:
        fcntl.flock(connect_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(32)
        result = {}
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                u = urlsplit(self.path)
                query = parse_qs(u.query)
                valid = (u.path == '/callback' and len(query.get('state', [])) == 1
                         and secrets.compare_digest(query['state'][0], state))
                if not valid:
                    self.send_error(400, 'Invalid OAuth state')
                    return
                if query.get('error'):
                    result['error'] = 'Доступ не предоставлен. Повтори подключение, когда будешь готов.'
                elif len(query.get('code', [])) == 1:
                    result['code'] = query['code'][0]
                else:
                    self.send_error(400, 'Missing code')
                    return
                message = b'Authorization received. Return to the VM terminal to confirm the channel. You can close this tab.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(message)))
                self.end_headers()
                self.wfile.write(message)
        with HTTPServer(('127.0.0.1', port), Callback) as server:
            server.timeout = 1
            redirect = f'http://127.0.0.1:{server.server_port}/callback'
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code',
                'scope': ' '.join(SCOPES), 'state': state, 'code_challenge': challenge,
                'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent'})
            print('Открой эту ссылку в браузере и выбери нужный YouTube-канал:\n' + url, flush=True)
            print('Если браузер на другом ПК, сначала создай SSH-туннель из инструкции docs/YOUTUBE.md.', flush=True)
            deadline = time.monotonic() + 600
            while not result and time.monotonic() < deadline:
                server.handle_request()
        if not result:
            raise ValueError('Истекло время ожидания авторизации (10 минут)')
        if result.get('error'):
            raise ValueError(result['error'])
        body = urlencode({'client_id': client['client_id'], 'client_secret': client['client_secret'],
                          'code': result['code'], 'code_verifier': verifier,
                          'redirect_uri': redirect, 'grant_type': 'authorization_code'}).encode()
        code, _, payload = transport('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, body)
        if code != 200:
            api_error(code, payload)
        token = decoded(payload)
        if not token.get('refresh_token') or not token.get('access_token'):
            raise ValueError('Google не предоставил постоянный доступ; повтори подключение с согласием')
        granted = set(token.get('scope', '').split())
        if not set(SCOPES).issubset(granted):
            raise ValueError('Не предоставлены оба разрешения YouTube: чтение канала и загрузка видео')
        # Query identity without replacing existing credentials before user confirmation.
        code, _, payload = transport('GET', 'https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true',
                                     {'Authorization': 'Bearer ' + token['access_token']}, None)
        if code != 200:
            api_error(code, payload)
        items = decoded(payload).get('items', [])
        if len(items) != 1:
            raise ValueError('Выбери ровно один YouTube-канал при авторизации')
        channel = {'id': items[0]['id'], 'title': items[0]['snippet']['title']}
        print(f"Канал: {channel['title']} ({channel['id']})", flush=True)
        if input_fn('Подключить именно этот канал? Введи YES: ').strip() != 'YES':
            raise ValueError('Подключение отменено; прежние настройки сохранены')
        data = {'client_id': client['client_id'], 'client_secret': client['client_secret'],
                'access_token': token['access_token'], 'refresh_token': token['refresh_token'],
                'expires_at': time.time() + int(token.get('expires_in', 3600)), 'channel': channel}
        with open(folder / 'token.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            save_json(folder / 'token.json', data)
        print('Канал подключён. Обнови вкладку YouTube в студии. Токен сохранён локально, права 600.')


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--client', type=Path, default=root/'client_secret.json')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Порт должен быть 1024..65535')
    data = Path(os.environ.get('LOFI_WEB_DATA', str(root/'web_data'))).expanduser()
    try:
        authorize(data/'youtube', args.client.expanduser(), args.port)
    except KeyboardInterrupt:
        raise SystemExit('Подключение отменено')
    except Exception as exc:
        # Do not print a traceback or OAuth HTTP bodies that might contain secrets.
        from youtube_upload import UploadError
        message = str(exc) if isinstance(exc, (ValueError, UploadError)) else 'Не удалось подключить канал. Проверь JSON клиента, SSH-туннель и доступ к Google.'
        raise SystemExit(message)

if __name__ == '__main__':
    main()
