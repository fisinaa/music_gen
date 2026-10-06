#!/usr/bin/env python3
"""Small authenticated local web studio; SQLite queue; one worker."""
import argparse, base64, contextlib, fcntl, hashlib, hmac, json, mimetypes, os, re
import secrets, shutil, signal, sqlite3, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
from presets import STYLES, MOODS, SCENES
from music_library import track_date

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('LOFI_WEB_DATA',str(ROOT/'web_data'))).expanduser().resolve()
MUSIC=Path(os.environ.get('LOFI_MUSIC_DIR',str(ROOT/'music'))).expanduser().resolve()
EPISODES=ROOT/'episodes'
STOP=threading.Event()
CHILD=None
CHILD_LOCK=threading.Lock()
PASSWORD=''
YOUTUBE=None

def read(path, default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default

def db():
    c=sqlite3.connect(DATA/'queue.sqlite',timeout=15);c.row_factory=sqlite3.Row
    return c

def init():
    DATA.mkdir(parents=True,exist_ok=True);os.chmod(DATA,0o700)
    (DATA/'logs').mkdir(exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,name TEXT UNIQUE,title TEXT,payload TEXT,status TEXT,created REAL,started REAL,finished REAL,error TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS preferences(sha TEXT PRIMARY KEY,favorite INTEGER DEFAULT 0,excluded INTEGER DEFAULT 0)')
        c.execute("INSERT OR IGNORE INTO settings VALUES('paused','0')")
        old=c.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        if old:
            c.execute("UPDATE jobs SET status='interrupted',error='Сервер панели перезапущен. Проверьте состояние ACE-Step перед повтором.' WHERE status='running'")
            c.execute("UPDATE settings SET value='1' WHERE key='paused'")
    password=DATA/'password'
    if not password.exists():
        fd=os.open(password,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(24)+'\n')
    os.chmod(password,0o600)
    return password.read_text().strip()

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def media_path(kind, value):
    if kind=='music':
        catalog=read(MUSIC/'catalog.json',{})
        record=catalog.get(value)
        if not record:raise ValueError('Трек не найден')
        base=MUSIC; path=base/record['filename']
    elif kind=='episode':
        if '/' not in value:raise ValueError('Файл не найден')
        name,file=value.split('/',1)
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name) or file not in ('episode.mp4','mix.wav','tracklist.txt','thumbnail.jpg','title.txt','description.txt','publication.json'):
            raise ValueError('Недопустимый путь')
        base=EPISODES;path=base/name/file
    else:raise ValueError('Недопустимый раздел')
    path=path.resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():raise ValueError('Файл не найден')
    if path.suffix not in ('.wav','.mp4','.txt','.jpg','.json'):raise ValueError('Недопустимый формат')
    return path

def library():
    catalog=read(MUSIC/'catalog.json',{})
    with db() as c:prefs={r['sha']:dict(r) for r in c.execute('SELECT * FROM preferences')}
    result=[]
    for sha,r in catalog.items():
        try:p=media_path('music',sha)
        except ValueError:continue
        pref=prefs.get(sha,{})
        timestamp,date_kind=track_date({**r,'sha256':sha},p)
        result.append({'sha':sha,'title':r.get('title',p.stem),'filename':p.name,
            'created_at':timestamp,'date_kind':date_kind,
            'seed':r.get('job',{}).get('seed'),'seconds':r.get('job',{}).get('duration_seconds'),
            'favorite':bool(pref.get('favorite')),'excluded':bool(pref.get('excluded'))})
    return sorted(result,key=lambda x:(-x['created_at'],x['title']))

def validate(payload):
    if not isinstance(payload,dict):raise ValueError('Ожидается объект')
    out={}
    out['mode']=payload.get('mode','create')
    if out['mode'] not in ('create','library'):raise ValueError('Недопустимый источник музыки')
    out['title']=str(payload.get('title','')).strip()[:100] or 'Retro Reverie Sounds'
    for key,choices,default in [('style',STYLES,'morning'),('mood',MOODS,'calm'),('time_of_day',SCENES,'morning')]:
        out[key]=payload.get(key,default)
        if out[key] not in choices:raise ValueError(f'Недопустимый параметр: {key}')
    for key,default,low,high in [('minutes',30,.5,180),('crossfade',6,1,12),('track_min',180,30,300),('track_max',240,30,300)]:
        v=payload.get(key,default)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not low<=v<=high:raise ValueError(f'Недопустимое значение: {key}')
        out[key]=v
    for key in ('track_min','track_max'):
        if int(out[key])!=out[key]:raise ValueError('Длительность трека должна быть целой')
        out[key]=int(out[key])
    if out['track_min']>out['track_max']:raise ValueError('Минимальная длительность больше максимальной')
    out['width']=payload.get('width',1600)
    if out['width'] not in (640,1280,1600,1920):raise ValueError('Неверное разрешение')
    out['audio_only']=payload.get('audio_only',False)
    if type(out['audio_only']) is not bool:raise ValueError('Неверный формат результата')
    bpm=payload.get('bpm')
    if bpm is not None and (type(bpm) is not int or not 40<=bpm<=180):raise ValueError('BPM должен быть 40–180')
    out['bpm']=bpm
    prompt=payload.get('custom_prompt','')
    if not isinstance(prompt,str) or len(prompt)>2000:raise ValueError('Промпт слишком длинный')
    out['custom_prompt']=prompt
    if out['mode']=='library':
        selected=payload.get('selected',[])
        if not isinstance(selected,list) or not 1<=len(selected)<=100 or len(set(map(str,selected)))!=len(selected):
            raise ValueError('Выберите от 1 до 100 разных треков')
        catalog=read(MUSIC/'catalog.json',{})
        for sha in selected:
            if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('Неверный ID трека')
            media_path('music',sha)
        out['selected']=selected
        # Capture metadata at submission time, not after later catalog edits.
        out['tracks']=[{'sha':s,'job':catalog[s].get('job',{})} for s in selected]
    return out

def enqueue(payload):
    p=validate(payload);id=uuid.uuid4().hex
    name='web_'+time.strftime('%Y%m%d_%H%M%S')+'_'+id[:8]
    with db() as c:
        c.execute('INSERT INTO jobs(id,name,title,payload,status,created) VALUES(?,?,?,?,?,?)',
                  (id,name,p['title'],json.dumps(p),'queued',time.time()))
    return id

def action(id, command, confirmed=False):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
        if not row:raise ValueError('Задача не найдена')
        if command=='cancel':
            if row['status']!='queued':raise ValueError('Можно отменить только ожидающую задачу')
            c.execute("UPDATE jobs SET status='cancelled',finished=? WHERE id=?",(time.time(),id))
        elif command=='retry':
            if row['status'] not in ('failed','interrupted'):raise ValueError('Задача не ожидает повтора')
            if confirmed is not True:raise ValueError('Подтвердите, что старая генерация в ACE-Step завершилась или сервер был перезапущен')
            p=json.loads(row['payload']);p['retry']=True
            c.execute("UPDATE jobs SET status='queued',payload=?,error=NULL,started=NULL,finished=NULL WHERE id=?",(json.dumps(p),id))
        else:raise ValueError('Неизвестное действие')

def claim():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]=='1':return None
        if c.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():return None
        row=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return None
        c.execute("UPDATE jobs SET status='running',started=? WHERE id=?",(time.time(),row['id']))
        return dict(row)

def prepare_command(row):
    p=json.loads(row['payload']);episode=EPISODES/row['name']
    cmd=[sys.executable,'-u',str(ROOT/'pipeline.py'),'create' if p['mode']=='create' else 'build',
        '--name',row['name'],'--minutes',str(p['minutes']),'--style',p['style'],'--mood',p['mood'],
        '--time-of-day',p['time_of_day'],'--width',str(p['width']),'--crossfade',str(p['crossfade']),
        '--track-min',str(p['track_min']),'--track-max',str(p['track_max']),'--music-dir',str(MUSIC)]
    if p.get('bpm'):cmd+=['--bpm',str(p['bpm'])]
    if p.get('custom_prompt'):cmd+=['--custom-prompt',p['custom_prompt']]
    if p['audio_only']:cmd+=['--audio-only']
    if p.get('retry'):cmd+=['--retry-failed','--retry-interrupted']
    if p['mode']=='library':
        source=episode/'selected_tracks'
        for n,item in enumerate(p['tracks'],1):
            src=media_path('music',item['sha'])
            if digest(src)!=item['sha']:raise ValueError(f'Изменился исходный трек: {src.name}')
            folder=source/f'track_{n:03d}';folder.mkdir(parents=True,exist_ok=True)
            dst=folder/'audio.wav'
            if not dst.exists():
                tmp=folder/'audio.partial.wav';shutil.copyfile(src,tmp);os.replace(tmp,dst)
            if digest(dst)!=item['sha']:raise ValueError('Изменился сохранённый выбранный трек')
            (folder/'manifest.json').write_text(json.dumps({'status':'done','qa':{'sha256':item['sha']},'job':item['job']}))
        cmd+=['--tracks',str(source)]
    episode.mkdir(parents=True,exist_ok=True)
    (episode/'web_title.json').write_text(json.dumps({'title':row['title']},ensure_ascii=False))
    return cmd

def tail(path):
    try:
        with open(path,'rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-12000));return f.read().decode(errors='replace')
    except OSError:return ''

def worker():
    global CHILD
    while not STOP.is_set():
        # Wait while a CLI run holds the shared pipeline lock.
        with open(ROOT/'.pipeline.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:STOP.wait(2);continue
        row=claim()
        if not row:STOP.wait(1);continue
        status='failed';error=None
        log=DATA/'logs'/(row['id']+'.log')
        try:
            if shutil.disk_usage(ROOT).free < 2*1024**3:raise RuntimeError('Меньше 2 GB свободного места на диске')
            cmd=prepare_command(row)
            with open(log,'ab') as out:
                out.write(('\n--- Attempt '+time.ctime()+' ---\n').encode());out.flush()
                with CHILD_LOCK:
                    if STOP.is_set():raise RuntimeError('Панель останавливается')
                    CHILD=subprocess.Popen(cmd,stdout=out,stderr=subprocess.STDOUT,start_new_session=True,cwd=ROOT)
                    proc=CHILD
                code=proc.wait()
                with CHILD_LOCK:CHILD=None
            if STOP.is_set():status='interrupted';error='Панель остановлена; проверьте сервер ACE-Step перед повтором'
            elif code==0:status='succeeded'
            else:error=tail(log)[-2500:] or f'Exit code {code}'
        except Exception as exc:error=str(exc);status='interrupted' if STOP.is_set() else 'failed'
        with db() as c:
            c.execute('UPDATE jobs SET status=?,finished=?,error=? WHERE id=?',(status,time.time(),error,row['id']))
            if status!='succeeded':c.execute("UPDATE settings SET value='1' WHERE key='paused'")

def snapshot():
    with db() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]
        paused=c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]=='1'
    for r in rows:
        r['payload']=json.loads(r['payload'])
        r['phase']=read(EPISODES/r['name']/'status.json',{})
        r['log']=tail(DATA/'logs'/(r['id']+'.log')) if r['status'] in ('running','failed','interrupted') else ''
    episodes=[]
    for folder in sorted(EPISODES.glob('*'),reverse=True):
        if not folder.is_dir() or not re.fullmatch(r'[A-Za-z0-9_-]+',folder.name):continue
        state=read(folder/'status.json',{})
        if state.get('status')!='done':continue
        episodes.append({'name':folder.name,'title':read(folder/'web_title.json',{}).get('title',folder.name),
            'video':(folder/'episode.mp4').is_file(),'audio':(folder/'mix.wav').is_file(),
            'seconds':state.get('duration_seconds')})
    free=shutil.disk_usage(ROOT).free/1024**3
    return {'jobs':rows,'paused':paused,'episodes':episodes[:100],'free_gb':round(free,1),
            'presets':{'styles':STYLES,'moods':MOODS,'scenes':SCENES}}

class Handler(BaseHTTPRequestHandler):
    server_version='RetroReverieStudio'
    def log_message(self,*args):pass
    def auth(self):
        value=self.headers.get('Authorization','')
        try:credentials=base64.b64decode(value.split(' ',1)[1],validate=True).decode() if value.startswith('Basic ') else ''
        except Exception:credentials=''
        if hmac.compare_digest(credentials.encode(),('admin:'+PASSWORD).encode()):return True
        self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Retro Reverie Studio", charset="UTF-8"');self.end_headers();return False
    def send_json(self,value,status=200):
        body=json.dumps(value,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def send_file(self,path):
        size=path.stat().st_size;start=0;end=size-1;partial=False
        header=self.headers.get('Range')
        if header:
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not m or not any(m.groups()):self.send_error(416);return
            if m[1]:start=int(m[1]);end=min(end,int(m[2])) if m[2] else end
            else:start=max(0,size-int(m[2]))
            if start>=size or start>end:self.send_error(416);return
            partial=True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
        self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command=='HEAD':return
        with open(path,'rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                chunk=f.read(min(1024*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        if not self.auth():return
        path=unquote(urlparse(self.path).path)
        try:
            if path=='/api/state':self.send_json(snapshot())
            elif path=='/api/music':self.send_json(library())
            elif path=='/api/youtube':self.send_json(YOUTUBE.public())
            elif path in ('/','/app.js','/style.css'):
                self.send_file(ROOT/'web'/({'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]))
            elif path.startswith('/media/'):
                kind,value=path[len('/media/'):].split('/',1);self.send_file(media_path(kind,value))
            else:self.send_error(404)
        except (ValueError,FileNotFoundError):self.send_json({'error':'Не найдено'},404)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_POST(self):
        if not self.auth():return
        try:
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):raise ValueError('Origin mismatch')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON required')
            size=int(self.headers.get('Content-Length',0))
            if not 0<size<=65536:raise ValueError('Неверный размер запроса')
            p=json.loads(self.rfile.read(size));path=urlparse(self.path).path
            if not isinstance(p,dict):raise ValueError('Ожидается JSON-объект')
            if path=='/api/jobs':self.send_json({'id':enqueue(p)},201)
            elif path=='/api/queue':
                if type(p.get('paused')) is not bool:raise ValueError('paused required')
                with db() as c:c.execute("UPDATE settings SET value=? WHERE key='paused'",('1' if p['paused'] else '0',))
                self.send_json({'ok':True})
            elif path=='/api/job-action':
                action(p.get('id'),p.get('action'),p.get('confirmed'));self.send_json({'ok':True})
            elif path=='/api/youtube/upload':
                if p.get('confirmed') is not True:raise ValueError('Подтверди загрузку приватного видео')
                ident=YOUTUBE.enqueue(p.get('name'),p.get('channel_id'),p.get('made_for_kids'),p.get('synthetic'))
                self.send_json({'id':ident},201)
            elif path=='/api/youtube/action':
                YOUTUBE.action(p.get('id'),p.get('action'));self.send_json({'ok':True})
            elif path=='/api/publication':
                from episode_package import prepare
                name=p.get('name','')
                if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',name):raise ValueError('Неверный выпуск')
                episode=(EPISODES/name).resolve()
                if not episode.is_relative_to(EPISODES.resolve()) or not episode.is_dir():raise ValueError('Выпуск не найден')
                self.send_json(prepare(ROOT,episode,p.get('edits')))
            elif path=='/api/preference':
                sha=p.get('sha');media_path('music',sha)
                if type(p.get('favorite')) is not bool or type(p.get('excluded')) is not bool:raise ValueError('Invalid preferences')
                with db() as c:c.execute('INSERT OR REPLACE INTO preferences VALUES(?,?,?)',(sha,int(p['favorite']),int(p['excluded'])))
                self.send_json({'ok':True})
            else:self.send_json({'error':'Не найдено'},404)
        except (ValueError,TypeError,KeyError) as e:self.send_json({'error':str(e)},400)
        except Exception as e:self.send_json({'error':str(e)},500)

class Server(ThreadingHTTPServer):
    daemon_threads=True

def main():
    global PASSWORD, YOUTUBE
    ap=argparse.ArgumentParser();ap.add_argument('--host',default=os.environ.get('LOFI_WEB_HOST','127.0.0.1'))
    ap.add_argument('--port',type=int,default=int(os.environ.get('LOFI_WEB_PORT','7861')))
    ap.add_argument('--init-only',action='store_true');a=ap.parse_args()
    DATA.mkdir(parents=True,exist_ok=True)
    lock=open(DATA/'server.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Web studio already running')
    PASSWORD=init()
    if a.init_only:print('Studio initialized. Password file:',DATA/'password');return
    from youtube_upload import Uploader
    YOUTUBE=Uploader(ROOT,DATA,EPISODES,STOP)
    server=Server((a.host,a.port),Handler);server.timeout=1
    thread=threading.Thread(target=worker,daemon=True);thread.start()
    youtube_thread=threading.Thread(target=YOUTUBE.run,daemon=True);youtube_thread.start()
    def stop(*_):
        STOP.set()
        with CHILD_LOCK:
            if CHILD and CHILD.poll() is None:
                with contextlib.suppress(ProcessLookupError):os.killpg(CHILD.pid,signal.SIGTERM)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(f'Studio listening on {a.host}:{a.port}; login admin; password file {DATA/"password"}',flush=True)
    try:
        while not STOP.is_set():server.handle_request()
    finally:
        stop();server.server_close();thread.join(timeout=10);youtube_thread.join(timeout=5)

if __name__=='__main__':main()
