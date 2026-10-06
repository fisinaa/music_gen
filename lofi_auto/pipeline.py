#!/usr/bin/env python3
"""Local, resumable ACE-Step music -> cafe video pipeline. No publishing."""
import argparse, contextlib, fcntl, hashlib, json, math, os, random, re
import shutil, subprocess, sys, time
from pathlib import Path
from urllib.request import urlopen
import numpy as np
from presets import STYLES, MOODS, SCENES, templates

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.0'
FPS = 24

def read(p):
    return json.loads(Path(p).read_text())

def write(p, value):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, p)

def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''): h.update(block)
    return h.hexdigest()

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

def run(cmd, log=None):
    print('  ' + (str(log.name) if log else str(cmd[0])), flush=True)
    if log:
        with open(log, 'w') as f:
            result = subprocess.run(list(map(str, cmd)), stdout=f, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'Command failed; see {log}\n{log.read_text()[-2000:]}')
    else:
        subprocess.run(list(map(str, cmd)), check=True)

def ff(args):
    return ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-threads', '4',
            '-filter_threads', '2', '-filter_complex_threads', '2', *args]

def probe(p):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
        '-show_streams', '-of', 'json', str(p)]))

def duration(p):
    return float(probe(p)['format']['duration'])

def finished(p, key):
    meta = p.with_name(p.name + '.stage.json')
    if not p.exists() or not meta.exists(): return False
    old = read(meta)
    return old.get('key') == key and old.get('sha256') == sha(p)

def commit(tmp, dest, key, seconds=None):
    if seconds is not None and abs(duration(tmp)-seconds) > .15:
        raise RuntimeError(f'Incorrect duration for {tmp}: expected {seconds}')
    os.replace(tmp, dest)
    write(dest.with_name(dest.name+'.stage.json'), {'key': key, 'sha256': sha(dest)})

def audio_qa(p):
    """Decode in bounded chunks; check finite data, silence, clipping and DC."""
    info = probe(p); streams = [s for s in info['streams'] if s['codec_type']=='audio']
    if len(streams)!=1: raise RuntimeError('Expected one audio stream')
    s = streams[0]
    if s['codec_name'] not in ('pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le'):
        raise RuntimeError('Expected an uncompressed WAV')
    if int(s['sample_rate']) != 48000 or s['channels'] != 2:
        raise RuntimeError('Expected 48000 Hz stereo')
    proc = subprocess.Popen(['ffmpeg','-v','error','-nostdin','-i',str(p),'-f','f32le',
        '-acodec','pcm_f32le','-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    total=0; sq=0.; sums=np.zeros(2); peak=0.; clipped=0; first=None; last=0
    quiet=0; longest=0; block_frames=4800; pending=b''; index=0
    try:
        while True:
            raw = proc.stdout.read(block_frames*2*4)
            if not raw: break
            raw = pending+raw; nbytes = len(raw)//8*8; pending = raw[nbytes:]
            x = np.frombuffer(raw[:nbytes],dtype='<f4').reshape(-1,2)
            if not len(x): continue
            if not np.isfinite(x).all(): raise RuntimeError('NaN/Inf in audio')
            xd=x.astype(np.float64); n=len(x); total+=n; sq+=float((xd*xd).sum())
            sums+=xd.sum(axis=0); peak=max(peak,float(np.abs(x).max()))
            clipped+=int((np.abs(x)>=.999).sum())
            if float(np.sqrt((xd*xd).mean())) < 10**(-60/20):
                quiet+=n; longest=max(longest,quiet)
            else:
                if first is None: first=index
                last=index+n; quiet=0
            index+=n
        if pending or proc.wait(): raise RuntimeError('Incomplete audio decode')
    finally:
        proc.stdout.close()
        if proc.poll() is None: proc.kill(); proc.wait()
    if not total or not sq or first is None: raise RuntimeError('Silent audio')
    rms=math.sqrt(sq/(total*2)); dc=float(np.max(np.abs(sums/total)))
    if rms<10**(-55/20): raise RuntimeError('Nearly silent audio')
    if dc>.15: raise RuntimeError('Excessive DC offset')
    if clipped/(total*2)>.005: raise RuntimeError('Excessive clipping')
    # Long internal silence is rejected; silent lead/tail is trimmed below.
    start=max(0, first/48000-.1); end=min(total/48000,last/48000+.2)
    if longest/48000 > max(8, start+.3, total/48000-end+.3):
        raise RuntimeError('Long silence; inspect track manually')
    return {'duration':total/48000,'start':start,'end':end,'usable':end-start,
            'rms_db':20*math.log10(rms),'peak':peak,'dc':dc,
            'clipping_fraction':clipped/(total*2),'sha256':sha(p)}

def loudness(p, log):
    result = subprocess.run(ff(['-i',str(p),'-af',
        'loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json','-f','null','-']),
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,check=True)
    log.write_text(result.stderr)
    matches=re.findall(r'\{\s*"input_i".*?\}',result.stderr,re.S)
    if not matches: raise RuntimeError(f'No loudness measurement; see {log}')
    values=json.loads(matches[-1])
    if not all(math.isfinite(float(values[k])) for k in ('input_i','input_tp','input_lra','input_thresh','target_offset')):
        raise RuntimeError('Invalid loudness measurement')
    return values

def pick_tracks(source, work, target, crossfade, music_dir=None):
    files=sorted(source.glob('track_*/audio.wav'))
    if not files: files=sorted(source.glob('*.wav'))
    if not files: raise RuntimeError(f'No track_*/audio.wav or WAV files in {source}')
    report=[]; selected=[]; total=0.; seen=set()
    for p in files:
        cache=work/'qa'/f'{fingerprint(str(p.resolve()))}.json'
        digest=sha(p)
        try:
            manifest=p.parent/'manifest.json'; job={}
            if p.name=='audio.wav' and manifest.exists():
                m=read(manifest)
                if m.get('status')!='done': raise RuntimeError('Queue track is not marked done')
                if m.get('qa',{}).get('sha256')!=digest: raise RuntimeError('Queue checksum mismatch')
                job=m.get('job',{})
            if digest in seen: raise RuntimeError('Duplicate audio skipped')
            seen.add(digest)
            cached=read(cache) if cache.exists() else {}
            q=cached['qa'] if cached.get('sha256')==digest and cached.get('version')==VERSION else audio_qa(p)
            write(cache,{'sha256':digest,'version':VERSION,'qa':q})
            if q['usable']<=crossfade*2+1: raise RuntimeError('Too short for crossfade')
            entry={'path':str(p.resolve()),'qa':q,'job':job,'status':'accepted'}
            if music_dir is not None:
                from music_library import export_track
                entry.update(export_track(p,music_dir,job))
            report.append(entry)
            if total < target:
                entry=dict(entry,start_in_mix=max(0,total-crossfade) if selected else 0)
                selected.append(entry); total+=q['usable']-(crossfade if len(selected)>1 else 0)
            print(f'CHECK {p.parent.name}: OK ({q["usable"]:.1f}s)',flush=True)
        except Exception as e:
            report.append({'path':str(p),'status':'rejected','reason':str(e)})
            print(f'REJECT {p}: {e}',flush=True)
    write(work/'quality_report.json',report)
    if total<target: raise RuntimeError(f'Only {total:.1f}s usable music; need {target:.1f}s. See quality_report.json')
    return selected

def make_audio(tracks, work, target, fade):
    prepared=[]
    for i,t in enumerate(tracks):
        p=Path(t['path']); q=t['qa']; dest=work/f'track_{i:03d}.wav'
        key=fingerprint([VERSION,q,'gain -19 LUFS peak -4'])
        if not finished(dest,key):
            level=loudness(p,work/f'track_{i:03d}_loudness.log')
            gain=min(-19-float(level['input_i']),-4-float(level['input_tp']))
            tmp=work/f'track_{i:03d}.partial.wav'
            run(ff(['-i',p,'-af',f'atrim=start={q["start"]}:end={q["end"]},asetpts=PTS-STARTPTS,volume={gain}dB',
                    '-ar','48000','-ac','2','-c:a','pcm_s16le',tmp]),work/f'track_{i:03d}.log')
            commit(tmp,dest,key,q['usable'])
        prepared.append(dest)
    raw=work/'mix_raw.wav'; key=fingerprint([VERSION,[sha(p) for p in prepared],target,fade])
    if not finished(raw,key):
        args=[]; filters=[]; previous='0:a'
        for p in prepared: args+=['-i',p]
        for i in range(1,len(prepared)):
            label=f'm{i}'; filters.append(f'[{previous}][{i}:a]acrossfade=d={fade}:c1=qsin:c2=qsin[{label}]');previous=label
        filters.append(f'[{previous}]atrim=duration={target},afade=t=in:d=1,afade=t=out:st={target-3}:d=3[out]')
        tmp=work/'mix_raw.partial.wav'
        run(ff([*args,'-filter_complex',';'.join(filters),'-map','[out]','-c:a','pcm_s16le',tmp]),work/'mix.log')
        commit(tmp,raw,key,target)
    final=work.parent/'mix.wav'; key=fingerprint([VERSION,sha(raw),'loudnorm -18 -1.5 11'])
    if not finished(final,key):
        v=loudness(raw,work/'mix_loudness.log')
        filt=('loudnorm=I=-18:TP=-1.5:LRA=11:linear=true:'
              f'measured_I={v["input_i"]}:measured_TP={v["input_tp"]}:measured_LRA={v["input_lra"]}:'
              f'measured_thresh={v["input_thresh"]}:offset={v["target_offset"]}:print_format=json')
        tmp=work/'mix_final.partial.wav'
        run(ff(['-i',raw,'-af',filt,'-ar','48000','-ac','2','-c:a','pcm_s16le',tmp]),work/'normalize.log')
        qa=audio_qa(tmp); write(work/'final_audio_qa.json',qa)
        commit(tmp,final,key,target)
    return final

def make_loop(index, dest, length, width):
    from PIL import Image
    from scene import draw_scene
    tmp=dest.with_name(dest.stem+'.partial.mp4'); height=width*9//16; seam=min(2,length/2)
    proc=subprocess.Popen(ff(['-v','error','-f','rawvideo','-pix_fmt','rgb24','-s',f'{width}x{height}',
        '-r',str(FPS),'-i','-','-an','-c:v','libx264','-preset','fast','-crf','20',
        '-pix_fmt','yuv420p','-threads','4','-video_track_timescale','24000',tmp]),stdin=subprocess.PIPE)
    try:
        for n in range(round(length*FPS)):
            t=seam+n/FPS; frame=draw_scene(index,t)
            if t>length:
                fraction=(t-length)/seam; fraction=fraction*fraction*(3-2*fraction)
                frame=Image.blend(frame,draw_scene(index,t-length),fraction)
            if frame.width!=width: frame=frame.resize((width,height),Image.Resampling.LANCZOS)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait(): raise RuntimeError('Loop encoder failed')
    except BaseException:
        with contextlib.suppress(Exception): proc.stdin.close()
        if proc.poll() is None: proc.kill();proc.wait()
        raise
    return tmp

def make_video(work, cache, target, width, loop_seconds, audio, time_of_day="cycle"):
    scenes=sorted((ROOT/'scenes').glob('*.png'))
    if len(scenes)!=5: raise RuntimeError('Expected five scenes')
    loops=[]
    for i in SCENES[time_of_day]["indices"]:
        p=scenes[i]
        key=fingerprint([VERSION,sha(p),sha(ROOT/'scene.py'),width,loop_seconds,FPS])
        dest=cache/f'loop_{i}_{key[:16]}.mp4'
        if not finished(dest,key):
            print(f'ANIMATE scene {i+1}/5 (cached for future episodes)',flush=True)
            tmp=make_loop(i,dest,loop_seconds,width);commit(tmp,dest,key,loop_seconds)
        loops.append(dest)
    parts=[]; segment=target/len(loops); transition=min(3,segment/4)
    for i,p in enumerate(loops):
        dest=work/f'video_{i}.mp4'
        key=fingerprint([VERSION,sha(p),sha(loops[(i+1)%len(loops)]),segment,transition,width,'phase-trim'])
        if not finished(dest,key):
            print(f'VIDEO scene {i+1}/{len(loops)}: {segment:.1f}s',flush=True)
            args=['-stream_loop','-1','-i',p]
            phase=transition if i else 0
            if i<len(loops)-1:
                args+=['-stream_loop','-1','-i',loops[i+1],'-filter_complex',
                    f'[0:v]trim=start={phase},settb=AVTB,setpts=PTS-STARTPTS[a];[1:v]settb=AVTB,setpts=PTS-STARTPTS[b];'
                    f'[a][b]xfade=transition=fade:duration={transition}:offset={segment-transition},format=yuv420p[v]',
                    '-map','[v]']
            else: args+=['-map','0:v:0','-vf',f'trim=start={phase},setpts=PTS-STARTPTS']
            tmp=work/f'video_{i}.partial.mp4'
            run(ff([*args,'-t',str(segment),'-an','-r',str(FPS),'-c:v','libx264',
                '-preset','fast','-crf','20','-pix_fmt','yuv420p','-threads','4',
                '-video_track_timescale','24000',tmp]),work/f'video_{i}.log')
            commit(tmp,dest,key,segment)
        parts.append(dest)
    listing=work/'concat.txt'
    # Paths are local fixed basenames, not user-controlled concat syntax.
    listing.write_text(''.join(f"file '{p.name}'\n" for p in parts))
    dest=work.parent/'episode.mp4'; key=fingerprint([VERSION,[sha(p) for p in parts],sha(audio)])
    if not finished(dest,key):
        tmp=work/'episode.partial.mp4'
        run(ff(['-f','concat','-safe','1','-i',listing,'-i',audio,'-map','0:v:0','-map','1:a:0',
            '-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-t',str(target),
            '-movflags','+faststart',tmp]),work/'mux.log')
        info=probe(tmp)
        if {s['codec_type'] for s in info['streams']}!={'video','audio'}: raise RuntimeError('Missing audio/video stream')
        # Full decode validates the assembled packet stream, not only container metadata.
        run(ff(['-v','error','-xerror','-i',tmp,'-f','null','-']),work/'video_verify.log')
        commit(tmp,dest,key,target)
    return dest

def server_ready(url):
    try:
        with urlopen(url.rstrip('/')+'/config', timeout=5) as r:
            config=json.load(r)
        return isinstance(config.get('dependencies'),list)
    except Exception: return False

def ensure_server(url):
    if server_ready(url): return
    if url.rstrip('/')!='http://127.0.0.1:7860': raise RuntimeError('Remote server unavailable; not starting a local replacement')
    print('START ACE-Step user service; initial model loading may take several minutes',flush=True)
    run(['systemctl','--user','start','lofi-ace.service'])
    deadline=time.time()+900
    while time.time()<deadline:
        if server_ready(url): return
        result=subprocess.run(['systemctl','--user','is-active','lofi-ace.service'],capture_output=True,text=True)
        if result.stdout.strip() not in ('active','activating'):
            raise RuntimeError('ACE-Step service stopped. Run: journalctl --user -u lofi-ace -n 80')
        print('  waiting for ACE-Step...',flush=True);time.sleep(10)
    raise RuntimeError('ACE-Step startup timeout; inspect journalctl --user -u lofi-ace')

def make_jobs(path, target, style="morning", mood="calm", bpm=None, custom_prompt="", track_min=180, track_max=240, crossfade=6):
    if path.exists(): return
    choices=templates(read(ROOT/'prompt_templates.json'),style,mood,bpm,custom_prompt); rng=random.Random(os.urandom(32)); jobs=[]; usable=0
    # A spare track accommodates trimming and occasional short outputs.
    while usable<target+240:
        j=dict(choices[len(jobs)%len(choices)]); j.update(number=len(jobs)+1,
            seed=rng.randrange(1,2**31),duration_seconds=rng.randint(track_min,track_max))
        jobs.append(j);usable+=j['duration_seconds']-crossfade
    write(path,jobs)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['build','create','collect'])
    ap.add_argument('--tracks',type=Path,help='Existing queue run or folder of 48 kHz stereo WAVs (build)')
    ap.add_argument('--name',help='Episode folder name, e.g. cafe_day_001')
    ap.add_argument('--music-dir',type=Path,default=Path(os.environ.get('LOFI_MUSIC_DIR',str(ROOT/'music'))))
    ap.add_argument('--style',choices=list(STYLES),default='morning')
    ap.add_argument('--mood',choices=list(MOODS),default='calm')
    ap.add_argument('--time-of-day',choices=list(SCENES),default='cycle')
    ap.add_argument('--bpm',type=int)
    ap.add_argument('--custom-prompt',default='')
    ap.add_argument('--track-min',type=int,default=180)
    ap.add_argument('--track-max',type=int,default=240)
    ap.add_argument('--audio-only',action='store_true')
    ap.add_argument('--minutes',type=float,default=30)
    ap.add_argument('--output',type=Path,default=ROOT/'episodes')
    ap.add_argument('--url',default='http://127.0.0.1:7860')
    ap.add_argument('--crossfade',type=float,default=6)
    ap.add_argument('--width',type=int,choices=[640,1280,1600,1920],default=1600)
    ap.add_argument('--loop-seconds',type=int,default=12)
    ap.add_argument('--retry-failed',action='store_true')
    ap.add_argument('--retry-interrupted',action='store_true')
    a=ap.parse_args()
    if a.command!='collect' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',a.name or ''): ap.error('Use letters, digits, _ or - in --name')
    if a.bpm is not None and not 40<=a.bpm<=180: ap.error('BPM must be 40..180')
    if not 30<=a.track_min<=a.track_max<=300: ap.error('Track durations must be 30..300, min <= max')
    if len(a.custom_prompt)>2000: ap.error('Custom prompt too long')
    if not .5<=a.minutes<=180: ap.error('--minutes must be 0.5..180')
    if not 1<=a.crossfade<=12: ap.error('--crossfade must be 1..12 seconds')
    if not 4<=a.loop_seconds<=60: ap.error('--loop-seconds must be 4..60')
    if a.command in ('build','collect') and not a.tracks: ap.error('build needs --tracks')
    if a.command=='create' and a.tracks: ap.error('create generates its own tracks; use build for existing files')
    for executable in ('ffmpeg','ffprobe'):
        if not shutil.which(executable): raise RuntimeError(f'Missing {executable}')
    if a.command=='collect':
        work=ROOT/'collection_reports'/fingerprint(str(a.tracks.expanduser().resolve()))[:16]
        work.mkdir(parents=True,exist_ok=True)
        lock=open(ROOT/'.pipeline.lock','w')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Another pipeline is running')
        pick_tracks(a.tracks.expanduser().resolve(),work,0,1,a.music_dir)
        accepted=[r for r in read(work/'quality_report.json') if r['status']=='accepted']
        if not accepted: raise RuntimeError(f'No valid tracks; see {work}/quality_report.json')
        print(f'COLLECTED {len(accepted)} tracks: {a.music_dir.resolve()}')
        return
    # Quantize to five equal scene lengths at 24 fps.
    target=round(a.minutes*60*FPS/5)*5/FPS
    episode=(a.output/a.name).resolve(); episode.mkdir(parents=True,exist_ok=True)
    cache=ROOT/'cache';cache.mkdir(exist_ok=True)
    # Serializes jobs and rendering across all episodes, avoiding GPU/CPU contention.
    lock=open(ROOT/'.pipeline.lock','w')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise RuntimeError('Another pipeline is running')
    work=episode/'work';work.mkdir(exist_ok=True)
    source=a.tracks.expanduser().resolve() if a.tracks else episode/'tracks'
    settings={'version':VERSION,'command':a.command,'source':str(source),'seconds':target,
              'crossfade':a.crossfade,'width':a.width,'loop_seconds':a.loop_seconds,'url':a.url}
    extras={'style':(a.style,'morning'),'mood':(a.mood,'calm'),'time_of_day':(a.time_of_day,'cycle'),
            'bpm':(a.bpm,None),'custom_prompt':(a.custom_prompt,''),'track_min':(a.track_min,180),
            'track_max':(a.track_max,240),'audio_only':(a.audio_only,False)}
    settings.update({k:v for k,(v,default) in extras.items() if v!=default})
    settings_file=episode/'settings.json'
    if settings_file.exists() and read(settings_file)!=settings:
        raise RuntimeError('Settings changed. Use a new --name; existing episode was not modified')
    write(settings_file,settings)
    state=episode/'status.json'
    try:
        if a.command=='create':
            write(state,{'stage':'generation','status':'running'})
            jobs=episode/'jobs.json';make_jobs(jobs,target,a.style,a.mood,a.bpm,a.custom_prompt,a.track_min,a.track_max,a.crossfade);ensure_server(a.url)
            args=[sys.executable,ROOT/'lofi_queue.py','run','--url',a.url,'--jobs',jobs,'--output',source,'--music-dir',a.music_dir.expanduser().resolve()]
            if a.retry_failed: args.append('--retry-failed')
            if a.retry_interrupted: args.append('--retry-interrupted')
            run(args)
        write(state,{'stage':'audio','status':'running'})
        tracks=pick_tracks(source,work,target,a.crossfade,a.music_dir)
        write(episode/'tracklist.json',tracks)
        listing=[]
        for i,t in enumerate(tracks):
            seconds=int(t['start_in_mix']); stamp=f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'
            listing.append(f'{stamp} {t.get("title",f"Track {i+1:02}")} — seed {t["job"].get("seed","unknown")}')
        (episode/'tracklist.txt').write_text('\n'.join(listing)+'\n')
        audio=make_audio(tracks,work,target,a.crossfade)
        if a.audio_only:
            write(state,{'stage':'complete','status':'done','audio':str(audio),'duration_seconds':target,'finished':time.time()})
            print(f'DONE: {audio}',flush=True);return
        write(state,{'stage':'video','status':'running'})
        video=make_video(work,cache,target,a.width,a.loop_seconds,audio,a.time_of_day)
        write(state,{'stage':'complete','status':'done','video':str(video),'audio':str(audio),
                     'duration_seconds':target,'finished':time.time()})
        print(f'\nDONE: {video}\nAUDIO: {audio}\nTRACKLIST: {episode/"tracklist.txt"}',flush=True)
    except BaseException as e:
        previous=read(state) if state.exists() else {}
        write(state,dict(previous,status='stopped',error=str(e),time=time.time()))
        raise

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('\nStopped; rerun the same command to resume. Check ACE queue state first.',file=sys.stderr);sys.exit(130)
    except Exception as e: print(f'STOP: {e}',file=sys.stderr);sys.exit(1)
