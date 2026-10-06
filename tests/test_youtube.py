"""Deterministic HTTP-protocol simulations: no real Google requests or credentials."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import urllib.request
import urllib.error

APP = Path(__file__).resolve().parents[1] / 'lofi_auto'
sys.path.insert(0, str(APP))
import youtube_upload as yt
import youtube_auth as auth
from PIL import Image


class FakeGoogle:
    """Imitates resumable HTTP semantics, including a commit with lost response."""
    def __init__(self):
        self.channel_id = 'UC_TEST_CHANNEL'
        self.inserts = 0
        self.received = bytearray()
        self.total = None
        self.metadata = None
        self.fail_final_once = False
        self.fail_thumbnail = False
        self.expired = False
        self.fail_init = False
        self.fail_chunk_once = False
        self.thumbnail_calls = 0
        self.refreshes = 0
        self.forced_401 = False
        self.video_id = 'AbC0123_-xy'
        self.requests = []

    def __call__(self, method, url, headers=None, body=None):
        headers = headers or {}
        self.requests.append((method, url))
        if url == yt.TOKEN_URL:
            self.refreshes += 1
            return 200, {}, json.dumps({'access_token':'FAKE_ACCESS','expires_in':3600}).encode()
        if self.forced_401:
            self.forced_401 = False
            return 401, {}, b'{}'
        assert headers.get('Authorization') == 'Bearer FAKE_ACCESS'
        if '/channels?' in url:
            return 200, {}, json.dumps({'items':[{'id':self.channel_id,'snippet':{'title':'Test channel'}}]}).encode()
        if method == 'POST' and '/videos?' in url:
            self.inserts += 1
            self.received = bytearray()
            if self.inserts > 1:self.video_id = f'Video{self.inserts:06d}'
            self.metadata = json.loads(body)
            self.total = int(headers['X-Upload-Content-Length'])
            if self.fail_init:
                raise yt.Retryable('test connection lost')
            return 200, {'Location':yt.UPLOAD+'videos?upload_id=FAKE_SESSION'}, b''
        if method == 'PUT':
            if self.expired:
                return 404, {}, b'{}'
            value = headers['Content-Range']
            if not value.startswith('bytes */'):
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', value)
                assert int(match[1]) == len(self.received), 'client must use confirmed server offset'
                assert int(match[2]) - int(match[1]) + 1 == len(body)
                self.received += body
                if self.fail_chunk_once:
                    self.fail_chunk_once = False
                    raise yt.Retryable('test accepted chunk, response lost')
            if len(self.received) == self.total:
                if self.fail_final_once:
                    self.fail_final_once = False
                    raise yt.Retryable('test accepted final chunk, response lost')
                return 201, {}, json.dumps({'id':self.video_id}).encode()
            return 308, ({'Range':f'bytes=0-{len(self.received)-1}'} if self.received else {}), b''
        if '/thumbnails/set?' in url:
            self.thumbnail_calls += 1
            if self.fail_thumbnail:
                return 403, {}, b'{"error":{"errors":[{"reason":"forbidden"}]}}'
            assert body[:2] == b'\xff\xd8'
            return 200, {}, b'{"items":[]}'
        raise AssertionError((method, url))


class FastStop(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.episodes = self.root/'episodes'
        self.episode = self.episodes/'test_episode'
        self.episode.mkdir(parents=True)
        (self.episode/'status.json').write_text('{"status":"done","duration_seconds":30}')
        (self.episode/'publication.json').write_text(json.dumps({'title':'Reviewed title','description':'Reviewed description'}))
        (self.episode/'episode.mp4').write_bytes(b'fake MP4 content'*50000)
        Image.new('RGB',(1280,720),(20,30,40)).save(self.episode/'thumbnail.jpg')
        self.fake = FakeGoogle()
        self.folder = self.root/'data'/'youtube'
        yt.save_json(self.folder/'token.json', {'client_id':'FAKE_CLIENT','client_secret':'FAKE_SECRET',
            'access_token':'FAKE_ACCESS','refresh_token':'FAKE_REFRESH','expires_at':time.time()+3600,
            'channel':{'id':self.fake.channel_id,'title':'Test channel'}})
        self.credentials = yt.Credentials(self.folder, self.fake)
        self.uploader = yt.Uploader(self.root, self.root/'data', self.episodes, FastStop(), self.credentials)

    def tearDown(self):
        self.tmp.cleanup()

    def enqueue(self):
        return self.uploader.enqueue('test_episode', self.fake.channel_id, False, True)

    def test_private_snapshot_and_dedup(self):
        ident = self.enqueue()
        self.assertEqual(ident, self.enqueue())
        # Later edits must not silently change approved upload metadata.
        (self.episode/'publication.json').write_text('{"title":"Later edit","description":"Later edit"}')
        self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'done')
        self.assertEqual(self.fake.metadata['snippet']['title'], 'Reviewed title')
        self.assertEqual(self.fake.metadata['status'], {'privacyStatus':'private','selfDeclaredMadeForKids':False,'containsSyntheticMedia':True})
        self.assertEqual(bytes(self.fake.received), (self.episode/'episode.mp4').read_bytes())
        self.assertEqual(self.fake.inserts, 1)
        self.assertEqual(self.enqueue(), ident)
        public = json.dumps(self.uploader.public())
        for secret in ('FAKE_SECRET','FAKE_REFRESH','FAKE_ACCESS','FAKE_SESSION'):
            self.assertNotIn(secret, public)
        self.assertEqual((self.folder/'token.json').stat().st_mode & 0o777, 0o600)

    def test_reupload_new_video_current_metadata_keeps_history(self):
        original=self.enqueue();self.uploader.run_one(original)
        before=self.uploader.get(original)
        (self.episode/'publication.json').write_text('{"title":"New title","description":"Updated description"}')
        new=self.uploader.reupload(original,self.fake.channel_id)
        self.assertNotEqual(new,original)
        self.assertEqual(self.uploader.reupload(original,self.fake.channel_id),new)
        self.assertEqual(self.enqueue(),new)
        self.assertIsNone(self.uploader.get(new)['session'])
        self.uploader.run_one(new)
        after=self.uploader.get(new)
        self.assertEqual(after['status'],'done')
        self.assertNotEqual(after['video_id'],before['video_id'])
        self.assertEqual(self.fake.metadata['snippet']['title'],'New title')
        self.assertEqual(self.fake.metadata['status']['privacyStatus'],'private')
        self.assertEqual(self.uploader.get(original),before)
        self.assertEqual(self.fake.inserts,2)
        self.assertTrue(next(r for r in self.uploader.public()['uploads'] if r['id']==original)['has_repeat'])
        third=self.uploader.reupload(new,self.fake.channel_id)
        self.assertNotEqual(third,new)

    def test_reupload_active_guard_and_concurrent_requests(self):
        original=self.enqueue()
        with self.assertRaises(ValueError):self.uploader.reupload(original,self.fake.channel_id)
        self.uploader.update(original,status='needs_review')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            ids=list(pool.map(lambda _:self.uploader.reupload(original,self.fake.channel_id),range(2)))
        self.assertEqual(ids[0],ids[1])
        with self.assertRaises(ValueError):self.uploader.reupload(original,'UC_WRONG')
        with self.assertRaises(ValueError):self.uploader.action(original,'retry')
        self.assertEqual(len(self.uploader.public()['uploads']),2)

    def test_legacy_database_migration_preserves_session(self):
        original=self.enqueue()
        self.uploader.update(original,status='failed',session=yt.UPLOAD+'videos?upload_id=OLD',offset=42)
        before=self.uploader.get(original)
        columns='id,episode,channel_id,video_sha,size,payload,status,session,video_id,offset,error,created,updated'
        with self.uploader.db() as c:
            c.execute('ALTER TABLE uploads RENAME TO new_uploads')
            c.execute('''CREATE TABLE uploads(id TEXT PRIMARY KEY,episode TEXT,channel_id TEXT,video_sha TEXT,
                size INTEGER,payload TEXT,status TEXT,session TEXT,video_id TEXT,offset INTEGER DEFAULT 0,
                error TEXT,created REAL,updated REAL,UNIQUE(channel_id,video_sha))''')
            c.execute(f'INSERT INTO uploads({columns}) SELECT {columns} FROM new_uploads')
            c.execute('DROP TABLE new_uploads')
        migrated=yt.Uploader(self.root,self.root/'data',self.episodes,FastStop(),self.credentials)
        self.assertEqual(migrated.get(original),before)
        again=yt.Uploader(self.root,self.root/'data',self.episodes,FastStop(),self.credentials)
        self.assertEqual(again.get(original),before)
        new=again.reupload(original,self.fake.channel_id)
        self.assertNotEqual(original,new)
        self.assertEqual(again.get(original)['offset'],42)
        with self.assertRaises(ValueError):again.action(original,'retry')

    def test_lost_final_response_recovers_same_video(self):
        ident = self.enqueue()
        self.fake.fail_final_once = True
        self.uploader.run_one(ident)
        row = self.uploader.get(ident)
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['video_id'], self.fake.video_id)
        self.assertEqual(self.fake.inserts, 1)

    def test_lost_chunk_response_uses_server_offset(self):
        ident = self.enqueue()
        self.fake.fail_chunk_once = True
        with patch.object(yt, 'CHUNK', 256*1024):
            self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'done')
        self.assertEqual(self.fake.inserts, 1)
        self.assertEqual(len(self.fake.received), (self.episode/'episode.mp4').stat().st_size)

    def test_thumbnail_retry_does_not_upload_video(self):
        ident = self.enqueue()
        self.fake.fail_thumbnail = True
        self.uploader.run_one(ident)
        row = self.uploader.get(ident)
        self.assertEqual(row['status'], 'thumbnail_failed')
        self.assertEqual(row['video_id'], self.fake.video_id)
        self.fake.fail_thumbnail = False
        self.uploader.action(ident, 'retry')
        self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'done')
        self.assertEqual(self.fake.inserts, 1)
        self.assertEqual(self.fake.thumbnail_calls, 2)

    def test_expired_session_never_reinserts(self):
        ident = self.enqueue()
        self.fake.expired = True
        self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'needs_review')
        self.assertEqual(self.fake.inserts, 1)
        with self.assertRaises(ValueError):
            self.uploader.action(ident, 'retry')

    def test_ambiguous_initialization_blocks_retry(self):
        ident = self.enqueue()
        self.fake.fail_init = True
        self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'needs_review')
        self.assertEqual(self.fake.inserts, 1)

    def test_changed_file_and_wrong_channel_block_upload(self):
        ident = self.enqueue()
        (self.episode/'episode.mp4').write_bytes(b'changed')
        self.uploader.run_one(ident)
        self.assertEqual(self.uploader.get(ident)['status'], 'failed')
        self.assertEqual(self.fake.inserts, 0)
        self.fake.channel_id = 'UC_DIFFERENT'
        self.uploader.run_one(ident)
        self.assertEqual(self.fake.inserts, 0)

    def test_restart_resumes_persisted_session(self):
        ident = self.enqueue()
        self.fake.fail_thumbnail = True
        self.uploader.run_one(ident)
        self.fake.fail_thumbnail = False
        self.uploader.update(ident, status='thumbnail')
        stop = threading.Event()
        another = yt.Uploader(self.root,self.root/'data',self.episodes,stop,self.credentials)
        thread = threading.Thread(target=another.run);thread.start()
        try:
            deadline = time.time()+5
            while another.get(ident)['status']!='done' and time.time()<deadline:
                time.sleep(.05)
            self.assertEqual(another.get(ident)['status'],'done')
            self.assertEqual(self.fake.inserts,1)
        finally:
            stop.set();thread.join(5)

    def test_refresh_and_endpoint_validation(self):
        self.fake.forced_401 = True
        self.credentials.channel()
        self.assertEqual(self.fake.refreshes,1)
        for url in ('http://www.googleapis.com/upload', 'https://attacker.invalid/upload', 'https://www.googleapis.com@attacker.invalid/', 'https://www.googleapis.com:444/upload'):
            with self.assertRaises(yt.UploadError):
                self.credentials.request('PUT',url,{},b'')
        with self.assertRaises(ValueError):
            self.uploader.enqueue('../escape',self.fake.channel_id,False,True)

    def test_http_transport_does_not_follow_redirects(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_PUT(self):
                self.send_response(308);self.send_header('Range','bytes=0-12');self.send_header('Location','http://127.0.0.1:1/private');self.end_headers()
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            with patch.object(yt,'google_url',lambda url:url):
                code,headers,body=yt.http('PUT',f'http://127.0.0.1:{server.server_port}/',{},b'')
            self.assertEqual(code,308);self.assertEqual(headers['Range'],'bytes=0-12')
        finally:
            server.shutdown();server.server_close();thread.join()


class OAuthTests(unittest.TestCase):
    def test_loopback_pkce_state_and_channel_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);client=root/'client.json'
            client.write_text('{"installed":{"client_id":"FAKE_CLIENT","client_secret":"FAKE_SECRET"}}')
            printed=[];printed_event=threading.Event();errors=[];seen={}
            def fake_print(*args,**kwargs):
                printed.append(' '.join(map(str,args)))
                if 'https://accounts.google.com' in printed[-1]:printed_event.set()
            def transport(method,url,headers,body):
                if url==yt.TOKEN_URL:
                    data=parse_qs(body.decode());seen.update(data)
                    return 200,{},json.dumps({'access_token':'FAKE_ACCESS','refresh_token':'FAKE_REFRESH','scope':' '.join(yt.SCOPES)}).encode()
                return 200,{},b'{"items":[{"id":"UC_TEST","snippet":{"title":"My channel"}}]}'
            def run():
                try:auth.authorize(root/'auth',client,0,transport,lambda _: 'YES')
                except Exception as exc:errors.append(exc)
            with patch('builtins.print',fake_print):
                thread=threading.Thread(target=run,daemon=True);thread.start()
                self.assertTrue(printed_event.wait(5))
                url=next(line.split('\n',1)[1] for line in printed if 'https://accounts.google.com' in line)
                q=parse_qs(urlsplit(url).query);callback=q['redirect_uri'][0]
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(callback+'?state=WRONG&code=fake')
                urllib.request.urlopen(callback+'?state='+q['state'][0]+'&code=fake').close()
                thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            verifier=seen['code_verifier'][0]
            challenge=auth.base64.urlsafe_b64encode(auth.hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(challenge,q['code_challenge'][0])
            stored=yt.read_json(root/'auth'/'token.json')
            self.assertEqual(stored['channel']['id'],'UC_TEST')
            self.assertEqual((root/'auth'/'token.json').stat().st_mode & 0o777,0o600)

if __name__ == '__main__':
    unittest.main()
