"""Flat WAV library with stable random titles and content-based deduplication."""
import fcntl, hashlib, json, os, random, shutil
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()

def export_track(source, folder, job=None):
    source=Path(source).resolve();folder=Path(folder).expanduser().resolve()
    folder.mkdir(parents=True,exist_ok=True)
    sha=digest(source)
    rng=random.Random(sha)
    adjective=rng.choice(['Quiet','Misty','Amber','Velvet','Gentle','Golden','Silver','Dreamy','Soft','Cozy','Lazy','Warm','Distant','Hidden','Sleepy','Tender'])
    noun=rng.choice(['Window','Morning','Rain','Coffee','Clouds','Streets','Breeze','Notebook','Horizon','Piano','Garden','Lantern','Moonlight','River','Terrace','Melody'])
    title=f'{adjective}_{noun}'
    with open(folder/'.catalog.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        index=folder/'catalog.json'
        catalog=json.loads(index.read_text()) if index.exists() else {}
        old=catalog.get(sha,{})
        name=old.get('filename',f'{title}_{sha[:10]}.wav')
        if Path(name).name!=name:raise RuntimeError('Invalid library filename')
        dest=folder/name
        if dest.exists() and digest(dest)!=sha:
            raise RuntimeError(f'Library file changed: {dest}; original was not overwritten')
        if not dest.exists():
            tmp=folder/(name+'.partial')
            shutil.copyfile(source,tmp)
            if digest(tmp)!=sha:raise RuntimeError('Source changed while copying')
            os.replace(tmp,dest)
        sources=list(dict.fromkeys(old.get('sources',[])+[str(source)]))
        record={'filename':name,'title':title.replace('_',' '),'sha256':sha,'sources':sources,
                'job':job or old.get('job',{})}
        catalog[sha]=record
        tmp=index.with_suffix('.tmp');tmp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,index)
    return {'library_path':str(dest),'title':record['title']}
