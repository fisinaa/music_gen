"""Integration tests use a temporary studio and synthetic WAVs; no ACE-Step request."""
import base64, concurrent.futures, importlib, json, math, os, shutil, struct, subprocess, sys
import tempfile, threading, time, unittest, urllib.request, urllib.error, wave
from pathlib import Path
APP=Path(__file__).resolve().parents[1]/'lofi_auto'
sys.path.insert(0,str(APP))
import web_server as web
from music_library import export_track

class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        app=self.root/'app';app.mkdir()
        for p in APP.glob('*.py'):shutil.copy2(p,app/p.name)
        shutil.copy2(APP/'prompt_templates.json',app/'prompt_templates.json')
        shutil.copytree(APP/'web',app/'web')
        web.ROOT=app;web.DATA=self.root/'data';web.MUSIC=self.root/'music';web.EPISODES=app/'episodes'
        web.STOP=threading.Event();web.CHILD=None;web.PASSWORD=web.init()
        from youtube_upload import Uploader
        web.YOUTUBE=Uploader(web.ROOT,web.DATA,web.EPISODES,web.STOP)
        self.auth='Basic '+base64.b64encode(('admin:'+web.PASSWORD).encode()).decode()
        self.server=web.Server(('127.0.0.1',0),web.Handler)
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.worker=None
    def tearDown(self):
        web.STOP.set()
        if self.worker:self.worker.join(15)
        self.server.shutdown();self.server.server_close();self.server_thread.join()
        self.tmp.cleanup()
    def request(self,path,p=None,auth=True,extra=None):
        headers={'Authorization':self.auth} if auth else {}
        if p is not None:headers['Content-Type']='application/json'
        headers.update(extra or {})
        r=urllib.request.Request(self.url+path,data=json.dumps(p).encode() if p is not None else None,headers=headers)
        with urllib.request.urlopen(r,timeout=10) as f:return f.status,f.read(),dict(f.headers)
    def api(self,path,p=None):return json.loads(self.request(path,p)[1])
    def track(self,freq=220,seconds=18):
        path=self.root/f'{freq}.wav';frames=bytearray()
        for n in range(seconds*48000):
            value=int(5000*math.sin(n*math.tau*freq/48000)*(0.8+0.2*math.sin(n/48000)))
            frames+=struct.pack('<hh',value,value)
        with wave.open(str(path),'wb') as f:f.setnchannels(2);f.setsampwidth(2);f.setframerate(48000);f.writeframes(frames)
        export_track(path,web.MUSIC,{'seed':freq,'duration_seconds':seconds})
        return web.digest(path)
    def test_music_dates_legacy_and_reimport(self):
        from music_library import track_date
        one=self.track(220,seconds=1)
        source=self.root/'220.wav'
        (self.root/'manifest.json').write_text(json.dumps({'status':'done','finished':1700000000,'qa':{'sha256':one}}))
        # Legacy catalog without timestamps still recovers the original date.
        item=next(t for t in self.api('/api/music') if t['sha']==one)
        self.assertEqual(item['created_at'],1700000000)
        self.assertEqual(item['date_kind'],'generation')
        export_track(source,web.MUSIC)
        (self.root/'manifest.json').unlink()
        export_track(source,web.MUSIC)
        self.assertEqual(next(t for t in self.api('/api/music') if t['sha']==one)['created_at'],1700000000)
        two=self.track(330,seconds=1)
        rows=self.api('/api/music')
        self.assertEqual(rows[0]['sha'],two)
        self.assertEqual(rows[0]['date_kind'],'file')
        self.assertEqual(track_date({'sha256':one,'sources':[str(source)]},source)[1],'file')

    def test_publication_for_existing_episode(self):
        from PIL import Image
        scenes=web.ROOT/'scenes';scenes.mkdir()
        for i in range(5):Image.new('RGB',(640,360),(30+i*20,55,70)).save(scenes/f'{i}.png')
        folder=web.EPISODES/'old_episode';folder.mkdir(parents=True)
        (folder/'status.json').write_text(json.dumps({'status':'done','duration_seconds':600}))
        (folder/'settings.json').write_text(json.dumps({'style':'morning','time_of_day':'morning'}))
        (folder/'tracklist.json').write_text(json.dumps([{'title':'Quiet Window','start_in_mix':0},{'title':'Amber Coffee','start_in_mix':182.5}]))
        data=self.api('/api/publication',{'name':'old_episode'})
        self.assertIn('Rainy Morning',data['title'])
        self.assertIn('03:02 Amber Coffee',data['description'])
        with Image.open(folder/'thumbnail.jpg') as img:self.assertEqual(img.size,(1280,720))
        edits={'title':'My edited title','description':'My saved description'}
        self.api('/api/publication',{'name':'old_episode','edits':edits})
        again=self.api('/api/publication',{'name':'old_episode'})
        self.assertEqual(again['title'],edits['title'])
        self.assertEqual(self.request('/media/episode/old_episode/title.txt')[1].decode().strip(),edits['title'])
        with self.assertRaises(urllib.error.HTTPError):self.api('/api/publication',{'name':'../private'})
        with self.assertRaises(urllib.error.HTTPError):self.api('/api/publication',{'name':'old_episode','edits':{'title':'x'*101,'description':''}})

    def test_long_queue_accounts_for_crossfade(self):
        import pipeline
        path=self.root/'jobs.json'
        pipeline.make_jobs(path,10800,track_min=30,track_max=30,crossfade=12)
        jobs=json.loads(path.read_text())
        self.assertGreaterEqual(sum(j['duration_seconds']-12 for j in jobs),10800)

    def test_auth_validation_and_range(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/state',auth=False)
        self.assertEqual(error.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as error:self.api('/api/jobs',{'minutes':-5})
        self.assertEqual(error.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/queue',{'paused':True},extra={'Origin':'http://evil.invalid'})
        sha=self.track(seconds=1)
        code,body,headers=self.request('/media/music/'+sha,extra={'Range':'bytes=0-31'})
        self.assertEqual(code,206);self.assertEqual(len(body),32);self.assertEqual(body[:4],b'RIFF')
        with self.assertRaises(urllib.error.HTTPError):self.request('/media/episode/../config.env')
        self.assertEqual(self.request('/')[0],200)
    def test_persistence_pause_cancel_claim_and_recovery(self):
        self.api('/api/queue',{'paused':True})
        first=self.api('/api/jobs',{'title':'First'})['id'];second=self.api('/api/jobs',{'title':'Second'})['id']
        self.assertIsNone(web.claim())
        self.api('/api/job-action',{'id':second,'action':'cancel'})
        self.api('/api/queue',{'paused':False})
        with concurrent.futures.ThreadPoolExecutor(2) as pool:claimed=list(pool.map(lambda _:web.claim(),range(2)))
        self.assertEqual(sum(r is not None for r in claimed),1)
        web.init();state=self.api('/api/state')
        self.assertTrue(state['paused']);self.assertEqual(next(j for j in state['jobs'] if j['id']==first)['status'],'interrupted')
        with self.assertRaises(urllib.error.HTTPError):self.api('/api/job-action',{'id':first,'action':'retry'})
        self.api('/api/job-action',{'id':first,'action':'retry','confirmed':True})
        self.assertEqual(next(j for j in self.api('/api/state')['jobs'] if j['id']==first)['status'],'queued')
    def test_full_video_package_and_youtube_api(self):
        from test_youtube import FakeGoogle
        import youtube_upload as yt
        shutil.copytree(APP/'scenes',web.ROOT/'scenes')
        fake=FakeGoogle()
        yt.save_json(web.YOUTUBE.folder/'token.json',{'client_id':'FAKE_CLIENT','client_secret':'FAKE_SECRET',
            'access_token':'FAKE_ACCESS','refresh_token':'FAKE_REFRESH','expires_at':time.time()+3600,
            'channel':{'id':fake.channel_id,'title':'Test channel'}})
        web.YOUTUBE.credentials=yt.Credentials(web.YOUTUBE.folder,fake)
        one=self.track(220);two=self.track(330)
        ident=self.api('/api/jobs',{'mode':'library','selected':[one,two],'minutes':.5,'crossfade':3,'width':640,'time_of_day':'morning'})['id']
        self.worker=threading.Thread(target=web.worker,daemon=True);self.worker.start()
        deadline=time.time()+120
        while time.time()<deadline:
            job=next(j for j in self.api('/api/state')['jobs'] if j['id']==ident)
            if job['status'] in ('succeeded','failed'):break
            time.sleep(.2)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        episode=web.EPISODES/job['name']
        duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(episode/'episode.mp4')],text=True))
        self.assertAlmostEqual(duration,30,places=1)
        self.assertTrue((episode/'publication.json').exists())
        self.api('/api/publication',{'name':job['name'],'edits':{'title':'Reviewed integration video','description':'Full pipeline test'}})
        payload={'name':job['name'],'channel_id':fake.channel_id,'made_for_kids':False,'synthetic':True,'confirmed':True}
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/youtube/upload',payload,auth=False)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/youtube/upload',payload,extra={'Origin':'http://evil.invalid'})
        with self.assertRaises(urllib.error.HTTPError):self.api('/api/youtube/upload',{**payload,'confirmed':False})
        upload=self.api('/api/youtube/upload',payload)['id']
        web.YOUTUBE.run_one(upload)
        self.assertEqual(self.api('/api/youtube')['uploads'][0]['status'],'done')
        self.assertEqual(fake.metadata['snippet']['title'],'Reviewed integration video')
        self.assertEqual(bytes(fake.received),(episode/'episode.mp4').read_bytes())
        self.assertEqual(self.api('/api/youtube/upload',payload)['id'],upload)
        self.assertEqual(fake.inserts,1)
        with self.assertRaises(urllib.error.HTTPError):self.api('/api/youtube/reupload',{'id':upload,'channel_id':fake.channel_id})
        new=self.api('/api/youtube/reupload',{'id':upload,'channel_id':fake.channel_id,'confirmed':True})['id']
        self.assertNotEqual(new,upload)
        self.assertEqual(self.api('/api/youtube/reupload',{'id':upload,'channel_id':fake.channel_id,'confirmed':True})['id'],new)
        web.YOUTUBE.run_one(new)
        self.assertEqual(fake.inserts,2)
        self.assertEqual(web.YOUTUBE.get(new)['status'],'done')
        for path in ('/media/episode/'+job['name']+'/token.json','/media/episode/../../web_data/youtube/token.json'):
            with self.assertRaises(urllib.error.HTTPError):self.request(path)

    def test_real_worker_builds_audio_from_library(self):
        one=self.track(220);two=self.track(330)
        self.api('/api/preference',{'sha':one,'favorite':True,'excluded':False})
        self.assertTrue(next(t for t in self.api('/api/music') if t['sha']==one)['favorite'])
        id=self.api('/api/jobs',{'mode':'library','selected':[one,two],'minutes':.5,'crossfade':3,'audio_only':True,'title':'Real worker test'})['id']
        self.worker=threading.Thread(target=web.worker,daemon=True);self.worker.start()
        deadline=time.time()+60
        while time.time()<deadline:
            job=next(j for j in self.api('/api/state')['jobs'] if j['id']==id)
            if job['status'] in ('succeeded','failed'):break
            time.sleep(.1)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        path=web.EPISODES/job['name']/'mix.wav'
        with wave.open(str(path)) as f:self.assertAlmostEqual(f.getnframes()/f.getframerate(),30,places=1)
        self.assertFalse((path.parent/'episode.mp4').exists())
        self.assertEqual(self.api('/api/state')['episodes'][0]['title'],'Real worker test')

if __name__=='__main__':unittest.main()
