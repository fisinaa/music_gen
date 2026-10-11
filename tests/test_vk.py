"""VK protocol and persistent queue tests; no real uploads."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lofi_auto'))
import vk_upload as vk

class VKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ep = self.root / 'episodes' / 'demo'; ep.mkdir(parents=True)
        (ep/'status.json').write_text('{"status":"done"}')
        (ep/'episode.mp4').write_bytes(b'video' * 100)
        self.u = vk.Uploader(self.root, self.root/'data', self.root/'episodes')
        vk.save_json(self.u.folder/'token.json', {'access_token':'SECRET', 'target':{'id':-123,'title':'Test'}})
        p = patch('episode_package.prepare', return_value={'title':'Title','description':'Description'})
        p.start(); self.addCleanup(p.stop)

    def test_success_and_dedup(self):
        ident = self.u.enqueue('demo', -123)
        self.assertEqual(ident, self.u.enqueue('demo', -123))
        def transfer(url, path, progress, stop):
            progress(500); return {'owner_id':-123,'video_id':99}
        with patch.object(vk, 'api', return_value={'owner_id':-123,'video_id':99,'upload_url':'https://pu.vk.com/upload'}) as api, patch.object(vk, 'upload', side_effect=transfer):
            self.u.run_one(ident)
        self.assertEqual(self.u.get(ident)['status'], 'done')
        self.assertEqual(api.call_args.kwargs['wallpost'], 0)
        self.assertEqual(api.call_args.kwargs['group_id'], 123)
        child = self.u.reupload(ident, -123)
        self.assertEqual(child, self.u.reupload(ident, -123))
        self.assertNotEqual(child, ident)
        self.assertNotIn('SECRET', json.dumps(self.u.public()))
        self.assertNotIn('upload_url', json.dumps(self.u.public()))

    def test_lost_save_reply_never_automatically_retries(self):
        ident = self.u.enqueue('demo', -123)
        with patch.object(vk, 'api', side_effect=vk.VKError('Lost reply')) as api:
            self.u.run_one(ident)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.u.get(ident)['status'], 'needs_review')
        self.u.recover()
        self.assertEqual(self.u.get(ident)['status'], 'needs_review')

    def test_lost_upload_reply_retains_video(self):
        ident = self.u.enqueue('demo', -123)
        with patch.object(vk, 'api', return_value={'owner_id':-123,'video_id':99,'upload_url':'https://pu.vk.com/upload'}), patch.object(vk, 'upload', side_effect=RuntimeError('SECRET')):
            self.u.run_one(ident)
        row = self.u.get(ident)
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(row['video_id'], 99)
        self.assertNotIn('SECRET', row['error'])

    def test_changed_account_and_file_block_before_remote_write(self):
        ident = self.u.enqueue('demo', -123)
        (self.root/'episodes/demo/episode.mp4').write_bytes(b'changed')
        with patch.object(vk, 'api') as api:
            self.u.run_one(ident)
        api.assert_not_called()
        self.assertEqual(self.u.get(ident)['status'], 'failed')
        with self.assertRaises(vk.VKError):self.u.enqueue('demo', -456)

    def test_restart_and_cancel(self):
        ident = self.u.enqueue('demo', -123)
        self.u.action(ident,'cancel')
        self.assertEqual(self.u.get(ident)['status'],'cancelled')
        child = self.u.reupload(ident,-123)
        self.u.update(child,status='uploading',video_id=99,owner_id=-123)
        self.u.recover()
        self.assertEqual(self.u.get(child)['status'],'needs_review')
        self.assertEqual(self.u.get(child)['video_id'],99)

    def test_upload_host_validation(self):
        for url in ['http://pu.vk.com/upload','https://vk.com.evil.test/upload','https://evil.test','https://u:p@vk.com/upload','https://vk.com:8443/upload']:
            with self.assertRaises(vk.VKError):vk.upload_address(url)
        vk.upload_address('https://pu.vkuser.net/upload?secret=x')

    def test_streaming_multipart(self):
        ident=self.u.enqueue('demo',-123)
        from unittest.mock import MagicMock
        conn=MagicMock(); response=conn.getresponse.return_value
        response.status=200;response.read.return_value=b'{"owner_id":-123,"video_id":99}'
        progress=[]
        with patch('http.client.HTTPSConnection',return_value=conn):
            result=vk.upload('https://pu.vk.com/upload',self.root/'episodes/demo/episode.mp4',progress.append,self.u.stop)
        body=b''.join(call.args[0] for call in conn.send.call_args_list)
        length=next(c.args[1] for c in conn.putheader.call_args_list if c.args[0]=='Content-Length')
        self.assertEqual(len(body),int(length));self.assertIn(b'name="video_file"',body)
        self.assertEqual(progress[-1],500);self.assertEqual(result['video_id'],99)

if __name__=='__main__':unittest.main()
