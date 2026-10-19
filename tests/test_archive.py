"""Archive lifecycle without credentials, networks or production deletions."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lofi_auto'))
import yandex_archive as ya
import youtube_upload as yt

class FakeDisk:
    def __init__(self):self.remote={};self.fail=False
    def mkdir(self,path):pass
    def meta(self,path):return self.remote.get(path)
    def verify(self,path,record):return not self.fail and self.remote.get(path)=={'type':'file','size':record['size'],'sha256':record['sha256']}
    def api(self,method,endpoint,**params):return 200,{'href':params['path']}
    def request(self,method,url,source=None):
        self.remote[url]={'type':'file','size':source.stat().st_size,'sha256':yt.digest(source)}
        return 201,{}
    archive=ya.Disk.archive

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ep=self.root/'episodes/demo';self.ep.mkdir(parents=True)
        yt.save_json(self.ep/'status.json',{'status':'done'})
        for name in ya.FILES:(self.ep/name).write_text(name)
        for name in ('work','tracks','selected_tracks'):
            (self.ep/name).mkdir();(self.ep/name/'audio.wav').write_bytes(b'audio')
        (self.ep/'mix.wav').write_bytes(b'mix')
        self.ar=ya.Archiver(self.root,self.root/'data',self.root/'episodes',[],threading.Event())
        self.ar.disk=FakeDisk()
        self.ar.prune_library=lambda episode:None

    def test_archive_then_clean_only_after_publication(self):
        with patch.object(self.ar,'published',return_value=False):self.ar.process(self.ep)
        self.assertTrue((self.ep/'episode.mp4').exists())
        self.assertEqual(yt.read_json(self.ep/'archive.json')['status'],'verified')
        with patch.object(self.ar,'published',return_value=True):self.ar.process(self.ep)
        for name in ('episode.mp4','mix.wav','tracks','work','selected_tracks'):self.assertFalse((self.ep/name).exists())
        self.assertTrue((self.ep/'thumbnail.jpg').exists())
        self.assertEqual(yt.read_json(self.ep/'archive.json')['status'],'archived')
        self.ar.process(self.ep) # Idempotent restart.

    def test_mismatch_or_remote_loss_preserves_local(self):
        self.ar.process(self.ep)
        self.ar.disk.fail=True
        with patch.object(self.ar,'published',return_value=True):self.ar.process(self.ep)
        self.assertTrue((self.ep/'episode.mp4').exists())
        self.assertTrue((self.ep/'tracks/audio.wav').exists())

    def test_no_uploads_is_not_published(self):self.assertFalse(self.ar.published('demo','sha'))

    def test_queued_failed_and_processing_block_cleanup(self):
        u=yt.Uploader(self.root,self.root/'data',self.root/'episodes')
        self.ar.uploaders=[u]
        with u.db() as db:
            db.execute("INSERT INTO uploads(id,episode,status,video_sha,video_id) VALUES('id','demo','queued','sha','abcdefghijk')")
        self.assertFalse(self.ar.published('demo','sha'))
        for status in ('failed','thumbnail_failed','needs_review'):
            u.update('id',status=status);self.assertFalse(self.ar.published('demo','sha'))
        u.update('id',status='done')
        with patch.object(u.credentials,'request',return_value=(200,{},b'{"items":[{"status":{"uploadStatus":"uploaded"}}]}')):
            self.assertFalse(self.ar.published('demo','sha'))
        with patch.object(u.credentials,'request',return_value=(200,{},b'{"items":[{"status":{"uploadStatus":"processed"}}]}')):
            self.assertTrue(self.ar.published('demo','sha'))

    def test_symlink_source_never_deleted(self):
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        import shutil
        shutil.rmtree(self.ep/'tracks');(self.ep/'tracks').symlink_to(outside,target_is_directory=True)
        with patch.object(self.ar,'published',return_value=True):self.ar.process(self.ep)
        self.assertTrue((outside/'keep').exists());self.assertTrue((self.ep/'episode.mp4').exists())

    def test_restore_verifies_and_reupload_wrapper_uses_it(self):
        self.ar.process(self.ep);record=yt.read_json(self.ep/'archive.json')['files']['episode.mp4']
        (self.ep/'episode.mp4').unlink();disk=ya.Disk(self.root/'data')
        def request(method,url,**kwargs):
            kwargs['destination'].write_text('episode.mp4');return 200,{}
        with patch.object(disk,'verify',return_value=True),patch.object(disk,'api',return_value=(200,{'href':'test'})),patch.object(disk,'request',side_effect=request):
            disk.restore(self.ep)
        self.assertEqual(yt.digest(self.ep/'episode.mp4'),record['sha256'])
        (self.ep/'episode.mp4').unlink()
        class Upload:
            folder=self.root/'data/youtube'
            def episode(inner,name):return self.ep
            @ya.with_local_video
            def enqueue(inner,name):return (self.ep/'episode.mp4').exists()
        with patch.object(ya.Disk,'restore',side_effect=lambda ep:(ep/'episode.mp4').write_text('restored')) as restore:
            self.assertTrue(Upload().enqueue('demo'));restore.assert_called_once()

    def test_prune_keeps_favorites_and_other_sources(self):
        import sqlite3
        music=self.root/'music';music.mkdir();data=self.root/'data';data.mkdir()
        with sqlite3.connect(data/'queue.sqlite') as db:
            db.execute('CREATE TABLE preferences(sha TEXT, favorite INTEGER)')
            db.execute('CREATE TABLE jobs(name TEXT,payload TEXT,status TEXT)')
            catalog={}
            for i,kind in enumerate(('owned','favorite','shared','queued')):
                path=music/(kind+'.wav');path.write_text(kind);sha=yt.digest(path)
                sources=[str(self.ep/'tracks'/kind)]
                if kind=='shared':sources.append(str(self.root/'other.wav'))
                catalog[sha]={'filename':path.name,'sources':sources}
                if kind=='favorite':db.execute('INSERT INTO preferences VALUES(?,1)',(sha,))
                if kind=='queued':db.execute('INSERT INTO jobs VALUES(?,?,?)',('other',json.dumps({'tracks':[{'sha':sha}]}),'queued'))
            yt.save_json(music/'catalog.json',catalog)
        ar=ya.Archiver(self.root,data,self.root/'episodes',[],threading.Event())
        with patch.dict('os.environ',{'LOFI_MUSIC_DIR':str(music)}):ar.prune_library(self.ep)
        self.assertFalse((music/'owned.wav').exists())
        for kind in ('favorite','shared','queued'):self.assertTrue((music/(kind+'.wav')).exists())
        self.assertEqual(len(yt.read_json(music/'catalog.json')),3)

    def test_corrupt_download_never_replaces_video(self):
        self.ar.process(self.ep);(self.ep/'episode.mp4').unlink();disk=ya.Disk(self.root/'data')
        def request(method,url,**kwargs):
            kwargs['destination'].write_text('corrupt');return 200,{}
        with patch.object(disk,'verify',return_value=True),patch.object(disk,'api',return_value=(200,{'href':'test'})),patch.object(disk,'request',side_effect=request):
            with self.assertRaises(ya.ArchiveError):disk.restore(self.ep)
        self.assertFalse((self.ep/'episode.mp4').exists())
        self.assertFalse((self.ep/'episode.restore.partial').exists())

    def test_manual_upload_is_durable_deduplicated_and_background(self):
        self.ar.disk.config=lambda: {'token':'test','enabled':False}
        self.assertEqual(self.ar.enqueue('demo')['status'],'queued')
        self.assertEqual(self.ar.enqueue('demo')['status'],'queued')
        self.assertTrue((self.ep/'archive_request.json').is_file())
        self.assertFalse(self.ar.disk.remote) # HTTP handler never transfers media.
        restarted=ya.Archiver(self.root,self.root/'data',self.root/'episodes',[],threading.Event())
        restarted.disk=self.ar.disk
        real_process=restarted.process
        def process(ep):
            real_process(ep);restarted.stop.set();restarted.wake.set()
        restarted.process=process
        restarted.run()
        self.assertFalse((self.ep/'archive_request.json').exists())
        self.assertEqual(yt.read_json(self.ep/'archive.json')['status'],'verified')
        self.assertTrue((self.ep/'episode.mp4').exists()) # No publication, no deletion.
        self.assertEqual(restarted.enqueue('demo')['status'],'verified')

    def test_manual_upload_checks_account_and_episode(self):
        self.ar.disk.config=lambda: {}
        with self.assertRaises(ya.ArchiveError):self.ar.enqueue('demo')
        self.ar.disk.config=lambda: {'token':'test'}
        with self.assertRaises(ya.ArchiveError):self.ar.enqueue('../demo')
        yt.save_json(self.ep/'status.json',{'status':'running'})
        with self.assertRaises(ya.ArchiveError):self.ar.enqueue('demo')
        self.assertFalse((self.ep/'archive_request.json').exists())

    def test_manual_delete_requires_archive_and_not_publication(self):
        with self.assertRaises(ya.ArchiveError):self.ar.delete_local('demo')
        self.assertTrue((self.ep/'tracks').exists())
        self.ar.process(self.ep)
        with patch.object(self.ar,'published',side_effect=AssertionError('Manual deletion does not require publication')):
            self.assertEqual(self.ar.delete_local('demo')['status'],'archived')
        self.assertFalse((self.ep/'episode.mp4').exists())
        self.assertFalse((self.ep/'tracks').exists())
        self.assertTrue((self.ep/'publication.json').exists())
        self.assertEqual(self.ar.delete_local('demo')['status'],'archived')

    def test_manual_delete_rechecks_remote(self):
        self.ar.process(self.ep);self.ar.disk.fail=True
        with self.assertRaises(ya.ArchiveError):self.ar.delete_local('demo')
        self.assertTrue((self.ep/'episode.mp4').exists())
        self.assertTrue((self.ep/'tracks').exists())

    def test_manual_delete_blocks_queued_upload(self):
        self.ar.process(self.ep)
        uploader=yt.Uploader(self.root,self.root/'data',self.root/'episodes')
        self.ar.uploaders=[uploader]
        with uploader.db() as db:
            db.execute("INSERT INTO uploads(id,episode,status) VALUES('queued','demo','queued')")
        with self.assertRaises(ya.ArchiveError):self.ar.delete_local('demo')
        self.assertTrue((self.ep/'episode.mp4').exists())
        uploader.update('queued',status='cancelled')
        self.ar.delete_local('demo')
        self.assertFalse((self.ep/'episode.mp4').exists())

    def test_manual_delete_blocks_active_generation(self):
        import fcntl
        self.ar.process(self.ep)
        with open(self.root/'.pipeline.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.assertRaises(ya.ArchiveError):self.ar.delete_local('demo')
        self.assertTrue((self.ep/'episode.mp4').exists())

    def test_manual_delete_rejects_bad_name(self):
        for name in ('../demo',None,''):
            with self.assertRaises(ya.ArchiveError):self.ar.delete_local(name)

    def test_incomplete_archive_never_cleans(self):
        (self.ep/'thumbnail.jpg').unlink()
        with patch.object(self.ar,'published',return_value=True):self.ar.process(self.ep)
        self.assertTrue((self.ep/'tracks').exists());self.assertTrue((self.ep/'episode.mp4').exists())

if __name__=='__main__':unittest.main()
