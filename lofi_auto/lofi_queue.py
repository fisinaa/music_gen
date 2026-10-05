#!/usr/bin/env python3
"""Sequential ACE-Step Gradio queue for the supplied 6.2.0 UI configuration."""
import argparse, copy, fcntl, hashlib, json, math, os, shutil, subprocess, sys, time
from pathlib import Path
from urllib.request import urlopen
from concurrent.futures import TimeoutError as FutureTimeout
ROOT=Path(__file__).resolve().parent

def read(p): return json.loads(Path(p).read_text())
def write(p,obj):
    p=Path(p); tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)); os.replace(tmp,p)
def dep(cfg,name):
    found=[d for d in cfg['dependencies'] if d.get('api_name')==name]
    if len(found)!=1: raise RuntimeError(f'Expected one endpoint {name}, got {len(found)}')
    return found[0]
def comps(cfg): return {c['id']:c for c in cfg['components']}
def public(cfg,name,side):
    c=comps(cfg)
    return [c[i] for i in dep(cfg,name)[side] if not c[i].get('skip_api',False) and c[i]['type']!='state']
def signature(cfg):
    return [(side,[(x['id'],x['type'],x.get('props',{}).get('label')) for x in public(cfg,'generation_wrapper',side)]) for side in ('inputs','outputs')]
def validate_job(j):
    if not isinstance(j.get('prompt'),str) or not j['prompt'].strip():raise ValueError('Empty prompt')
    if not isinstance(j.get('duration_seconds'),int) or not 30<=j['duration_seconds']<=300:raise ValueError('Duration must be 30..300 seconds')
    if j.get('steps')!=8 or j.get('batch_size')!=1:raise ValueError('Use 8 steps, batch=1')
    if j.get('instrumental') is not True:raise ValueError('Instrumental jobs only')
    if not isinstance(j.get('seed'),int) or j['seed']<0:raise ValueError('Need nonnegative integer seed')
    if not 40<=j.get('bpm',0)<=180:raise ValueError('Invalid BPM')
def arguments(cfg,j):
    validate_job(j)
    overrides={259:j['prompt'],264:'[Instrumental]',286:j['bpm'],287:'',288:'',289:'unknown',
      83:8,116:False,115:str(j['seed']),255:None,297:j['duration_seconds'],298:1,198:None,210:'',
      146:'wav',306:False,134:False,141:False,135:False,140:False,307:False,312:False,
      311:False,230:False,237:'',238:'',156:True,157:-1.0}
    inputs=public(cfg,'generation_wrapper','inputs')
    if not set(overrides)<=set(x['id'] for x in inputs):raise RuntimeError('Required input absent')
    return [overrides.get(x['id'],x.get('props',{}).get('value')) for x in inputs]
def audio_paths(value):
    if isinstance(value,str):
        try:
            p=Path(value)
            if p.suffix.lower()=='.wav' and p.is_file():yield p
        except (OSError,ValueError):pass
    elif isinstance(value,dict):
        for key in ('value','path','name'):
            if key in value:yield from audio_paths(value[key])
    elif isinstance(value,(tuple,list)):
        for item in value:yield from audio_paths(item)

def extract_audio(cfg,values):
    outputs=public(cfg,'generation_wrapper','outputs')
    if len(values)!=len(outputs):raise RuntimeError('Unexpected output count; see response.json')
    pairs=list(zip(outputs,values))
    statuses=[v for c,v in pairs if c['id'] in (480,490)]
    if any(isinstance(v,str) and ('error:' in v.lower() or 'generation failed' in v.lower()) for v in statuses):
        raise RuntimeError(str(statuses)[:1500])
    unique={}
    for c,value in pairs:
        # Gradio can return the WAV only in All Generated Files, not the players.
        if c['type']=='audio' or c['id']==489:
            for path in audio_paths(value):
                digest=hashlib.sha256(path.read_bytes()).hexdigest()
                unique.setdefault(digest,path)
    if len(unique)!=1:raise RuntimeError(f'Expected one distinct downloaded WAV, got {len(unique)}; see response.json')
    return next(iter(unique.values()))

def recover(cfg,jobs,output,limit=None):
    if not output.is_dir():raise RuntimeError('Result directory does not exist')
    lock=open(output/'.queue.lock','w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('Queue is running; stop it before recovery')
    count=0
    for j in jobs:
        folder=output/f"track_{j['number']:03d}_s{j['seed']}"
        manifest=folder/'manifest.json';response=folder/'response.json'
        if not manifest.exists():continue
        state=read(manifest)
        if state.get('status')=='done':continue
        if state.get('status')!='failed':raise RuntimeError(f'{folder.name}: state is not failed; server might still be running')
        if not response.exists():raise RuntimeError(f'{folder.name}: no saved response')
        fingerprint=hashlib.sha256(json.dumps(arguments(cfg,j),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if fingerprint!=state.get('fingerprint'):raise RuntimeError('Job settings changed; recovery stopped')
        source=extract_audio(cfg,read(response))
        temp=folder/'audio.partial.wav';shutil.copyfile(source,temp)
        qa=check_audio(temp,j['duration_seconds']);os.replace(temp,folder/'audio.wav')
        state['previous_error']=state.pop('error',None)
        state.update(status='done',qa=qa,finished=time.time(),recovered_from=str(source))
        write(manifest,state);count+=1
        print(f"RECOVERED {folder/'audio.wav'} ({qa['duration']:.1f}s)",flush=True)
        if limit is not None and count>=limit:break
    print(f'Recovered {count} tracks. No generation requests sent.')

def check_audio(path,expected=90):
    import numpy as np
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))
    streams=[s for s in probe['streams'] if s['codec_type']=='audio']
    if len(streams)!=1:raise RuntimeError('Expected single audio stream')
    s=streams[0]; duration=float(probe['format']['duration'])
    if not expected*.8<=duration<=expected*1.25:raise RuntimeError(f'Unexpected duration {duration:.2f}')
    if s['codec_name']!='pcm_s16le' or int(s['sample_rate'])!=48000 or s['channels']!=2:
        raise RuntimeError(f"Expected PCM16 WAV 48k stereo; got {s['codec_name']} / {s['sample_rate']} / {s['channels']}")
    data=subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-f','f32le','-acodec','pcm_f32le','-'])
    x=np.frombuffer(data,dtype='<f4')
    if not x.size or not np.isfinite(x).all():raise RuntimeError('Empty or non-finite audio')
    rms=float(np.sqrt(np.mean(x.astype('float64')**2))); peak=float(np.max(np.abs(x)))
    dc=float(abs(x.mean())); clipping=float(np.mean(np.abs(x)>=0.999))
    if rms<10**(-55/20):raise RuntimeError('Audio is effectively silent')
    if dc>.15:raise RuntimeError(f'Abnormal DC offset {dc:.3f}')
    if clipping>.005:raise RuntimeError(f'Excessive clipping {clipping:.2%}')
    return {'duration':duration,'rms_db':20*math.log10(rms),'peak':peak,'dc':dc,'clipping_fraction':clipping,
      'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['inspect','run','dry-run','recover'])
    ap.add_argument('--url',default='http://127.0.0.1:7860')
    ap.add_argument('--jobs',type=Path,default=ROOT/'music_jobs.json')
    ap.add_argument('--output',type=Path,default=ROOT/'runs'/'morning_01')
    ap.add_argument('--music-dir',type=Path,help='Export completed tracks to a flat named library')
    ap.add_argument('--limit',type=int,help='Maximum new tracks this invocation')
    ap.add_argument('--retry-failed',action='store_true')
    ap.add_argument('--retry-interrupted',action='store_true',help='Only after verifying old server job ended or restarting ACE-Step')
    a=ap.parse_args()
    if a.limit is not None and a.limit<1:ap.error('--limit must be positive')
    snapshot=read(ROOT/'api_snapshot.json'); jobs=read(a.jobs)
    if len({j['number'] for j in jobs})!=len(jobs):raise ValueError('Duplicate job numbers')
    for j in jobs:arguments(snapshot,j)
    if a.command=='dry-run':
        print(f'OK: {len(jobs)} jobs; {len(arguments(snapshot,jobs[0]))} API inputs; durations {min(j["duration_seconds"] for j in jobs)}..{max(j["duration_seconds"] for j in jobs)}s / WAV16 / batch1 / steps8 / LM off');return
    if a.command=='recover':
        recover(snapshot,jobs,a.output,a.limit);return
    with urlopen(a.url.rstrip('/')+'/config',timeout=20) as r:live=json.load(r)
    if signature(snapshot)!=signature(live):raise RuntimeError('API schema changed. Save fresh /config; queue stopped before generation.')
    a.output.mkdir(parents=True,exist_ok=True)
    lock=open(a.output/'.queue.lock','w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('A queue is already running in this output directory')
    for tool in ('ffmpeg','ffprobe'):
        if not shutil.which(tool):raise RuntimeError(f'Missing {tool}')
    from gradio_client import Client
    client=Client(a.url,download_files=str((a.output/'downloads').resolve()),verbose=False)
    api=client.view_api(print_info=False,return_format='dict')
    named=api.get('named_endpoints',{})
    for endpoint in ('/generation_wrapper','/_handle_mode_change','/clear_audio_outputs_for_new_generation'):
        if endpoint not in named:raise RuntimeError(f'API endpoint unavailable: {endpoint}')
    if len(named['/generation_wrapper']['parameters'])!=len(arguments(snapshot,jobs[0])):
        raise RuntimeError('Client argument count mismatch; no generation submitted')
    write(a.output/'api_info.json',api)
    if a.command=='inspect':print(f'API OK. {len(jobs)} jobs ready. Results: {a.output.resolve()}');return
    done=0
    for j in jobs:
        if a.limit is not None and done>=a.limit:break
        stem=f"track_{j['number']:03d}_s{j['seed']}"; folder=a.output/stem; folder.mkdir(exist_ok=True)
        manifest=folder/'manifest.json'; dest=folder/'audio.wav'
        args=arguments(snapshot,j)
        fingerprint=hashlib.sha256(json.dumps(args,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        old=read(manifest) if manifest.exists() else {}
        if old and old.get('fingerprint')!=fingerprint:raise RuntimeError(f'{stem}: settings changed; use a new output directory')
        if old.get('status')=='done':
            qa=check_audio(dest,j['duration_seconds'])
            if qa['sha256']!=old['qa']['sha256']:raise RuntimeError(f'{stem}: saved audio changed')
            if a.music_dir:
                from music_library import export_track
                export_track(dest,a.music_dir,j)
            print(f'SKIP {stem}: verified',flush=True);continue
        if old.get('status')=='failed' and not a.retry_failed:raise RuntimeError(f'{stem}: previous failure; read manifest and use --retry-failed after fixing')
        if old.get('status') in ('running','interrupted') and not a.retry_interrupted:
            raise RuntimeError(f'{stem}: previous submission may still be running. Verify it ended or restart ACE-Step, then --retry-interrupted')
        # Set Custom/text2music mode in this client's own server-side session.
        client.predict('Custom',api_name='/_handle_mode_change')
        client.predict(api_name='/clear_audio_outputs_for_new_generation')
        state={'status':'running','job':j,'fingerprint':fingerprint,'started':time.time()};write(manifest,state)
        print(f'GENERATE {stem}: {j["duration_seconds"]} seconds',flush=True)
        response_received=False
        try:
            job=client.submit(*args,api_name='/generation_wrapper')
            while True:
                try:result=job.result(timeout=30);break
                except FutureTimeout:
                    if job.done():raise
                    print(f'  waiting {time.time()-state["started"]:.0f}s',flush=True)
            response_received=True
            outputs=public(snapshot,'generation_wrapper','outputs')
            values=list(result) if isinstance(result,(tuple,list)) else [result]
            write(folder/'response.json',values)
            source=extract_audio(snapshot,values)
            temp=folder/'audio.partial.wav';shutil.copyfile(source,temp)
            qa=check_audio(temp,j['duration_seconds']);os.replace(temp,dest)
            state.update(status='done',qa=qa,finished=time.time());write(manifest,state)
            if a.music_dir:
                from music_library import export_track
                export_track(dest,a.music_dir,j)
            done+=1; print(f'SAVED {dest} ({qa["duration"]:.1f}s)',flush=True)
        except KeyboardInterrupt:
            state.update(status='interrupted',error='Client interrupted; server may still run');write(manifest,state)
            raise
        except Exception as e:
            if state.get('status')=='done':
                state.update(export_error=str(e));write(manifest,state)
                raise
            # A transport failure has an ambiguous server outcome; never resubmit automatically.
            uncertain=not response_received
            state.update(status='interrupted' if uncertain else 'failed',error=str(e));write(manifest,state)
            raise
    print(f'Completed {done} new tracks. Output: {a.output.resolve()}')

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('\nStopped. Check server before resuming.',file=sys.stderr);sys.exit(130)
    except Exception as e:print(f'STOP: {e}',file=sys.stderr);sys.exit(1)
