#!/usr/bin/env python3
"""Save/restore installed studio code; never roll back queues or upload history."""
import argparse, datetime, hashlib, json, os, shutil
from pathlib import Path

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def safe(root, name):
    p=(root/name).resolve()
    if not p.is_relative_to(root.resolve()):raise ValueError('Path outside checkpoint root')
    return p

def save(runtime, destination):
    if not (runtime/'pipeline.py').is_file():raise ValueError('Installed pipeline.py not found')
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    files=[p for p in runtime.iterdir() if p.is_file() and
        (p.suffix in ('.py','.sh') or p.name in ('lofi','config.env','prompt_templates.json','api_snapshot.json'))]
    for folder in ('web','scenes'):
        files += [p for p in (runtime/folder).rglob('*') if p.is_file()]
    manifest={'created':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':[]}
    for source in files:
        if source.is_symlink() or not source.resolve().is_relative_to(runtime.resolve()):raise ValueError('Unexpected symlink')
        name=source.relative_to(runtime).as_posix();target=destination/'files'/name
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(source,target);target.chmod(0o600)
        manifest['files'].append({'path':name,'sha256':sha(target),'mode':source.stat().st_mode&0o777})
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (destination/'manifest.json').chmod(0o600)
    print('Checkpoint:',destination)

def restore(runtime, checkpoint):
    manifest=json.loads((checkpoint/'manifest.json').read_text())
    entries=[]
    for item in manifest['files']:
        source=safe(checkpoint/'files',item['path']);target=safe(runtime,item['path'])
        # Code/config only; a checkpoint cannot revert media, databases or tokens.
        rel=Path(item['path'])
        allowed=(len(rel.parts)==1 and (rel.suffix in ('.py','.sh') or rel.name in
            ('lofi','config.env','prompt_templates.json','api_snapshot.json'))) or rel.parts[0] in ('web','scenes')
        if not allowed or sha(source)!=item['sha256']:raise ValueError('Invalid checkpoint: '+item['path'])
        entries.append((source,target,item['mode']))
    for source,target,mode in entries:
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.restore-tmp')
        shutil.copyfile(source,temp);temp.chmod(mode);os.replace(temp,target)
    print('Restored code and config. Queue, tokens, music and upload history were not changed.')

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--runtime',type=Path,default=Path.home()/'lofi_auto')
    ap.add_argument('--destination',type=Path)
    ap.add_argument('--restore',type=Path)
    a=ap.parse_args();os.umask(0o077)
    if a.restore:restore(a.runtime.expanduser().resolve(),a.restore.expanduser().resolve())
    else:save(a.runtime.expanduser().resolve(),(a.destination or
        Path.home()/'.local/state/lofi-checkpoints'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')).expanduser().resolve())
