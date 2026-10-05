#!/usr/bin/env python3
import ast,datetime,hashlib,json,os,shutil,sys
from pathlib import Path
P=Path(__file__).resolve().parent
repo=Path(sys.argv[1]).expanduser() if len(sys.argv)>1 else Path.home()/'ACE-Step-1.5'
target=repo/'acestep/core/generation/handler/generate_music_decode.py'
checks=json.loads((P/'checks.json').read_text());digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
if digest(target)==checks['patched_sha256']:print('Already installed');sys.exit(0)
if digest(target)!=checks['original_sha256']:sys.exit('STOP: current file differs from supplied version. Nothing changed.')
helper_file=target.with_name('vae_decode_chunks.py')
helper=next((n for n in ast.walk(ast.parse(helper_file.read_text())) if isinstance(n,ast.FunctionDef) and n.name=='_tiled_decode_offload_cpu'),None)
if helper is None or hashlib.sha256(ast.dump(helper,include_attributes=False).replace(", type_params=[]", "").encode()).hexdigest()!=checks['helper_ast_sha256']:
 sys.exit('STOP: tiled decode helper differs. Nothing changed. Send vae_decode_chunks.py.')
new=P/'generate_music_decode.py'
if digest(new)!=checks['patched_sha256']:sys.exit('STOP: patch file checksum mismatch')
compile(new.read_text(),str(target),'exec')
backup=target.with_name(target.name+'.before-gpu-vae-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
shutil.copy2(target,backup)
temp=target.with_name(target.name+'.gpu-vae.tmp');shutil.copy2(new,temp);os.chmod(temp,target.stat().st_mode);os.replace(temp,target)
print('Installed:',target);print('Backup:',backup)
