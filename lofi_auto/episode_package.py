"""Local, deterministic episode artwork and editable English publication text."""
import fcntl, json, os, re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from presets import SCENES, STYLES

SCENE_NAMES={'morning':'Rainy Morning Café','late_morning':'Slow Morning Café','day':'Afternoon Café',
             'sunset':'Sunset Café','evening':'Rainy Evening Café','cycle':'A Day at the Café'}
STYLE_NAMES={'morning':'Mellow Jazz','lofi':'Lo-fi Jazz','chillout':'Coastal Chillout','lounge':'Sunset Lounge','ambient':'Ambient Piano'}

def read(path, default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def atomic(path, content):
    tmp=path.with_name(path.name+'.partial')
    tmp.write_text(content,encoding='utf-8');os.replace(tmp,path)

def validate(title, description):
    if not isinstance(title,str) or not 1<=len(title.strip())<=100:raise ValueError('Название: от 1 до 100 символов')
    if not isinstance(description,str) or len(description)>5000:raise ValueError('Описание: до 5000 символов')
    if any(c in title+description for c in ('<','>','\x00')):raise ValueError('Уберите символы <, > и NUL')
    return title.strip(),description.strip()

def stamp(seconds):
    s=max(0,int(seconds));return f'{s//3600}:{s//60%60:02}:{s%60:02}' if s>=3600 else f'{s//60:02}:{s%60:02}'

def artwork(root, episode, scene, style):
    images=sorted((root/'scenes').glob('*.png'))
    if len(images)!=5:raise ValueError('Для обложки нужны пять фонов кафе')
    img=ImageOps.fit(Image.open(images[SCENES[scene]['indices'][0]]).convert('RGB'),(1280,720))
    overlay=Image.new('RGBA',img.size,(0,0,0,0));draw=ImageDraw.Draw(overlay)
    for y in range(720):draw.line((0,y,1280,y),fill=(8,17,27,int(30+170*y/720)))
    img=Image.alpha_composite(img.convert('RGBA'),overlay);draw=ImageDraw.Draw(img)
    def font(size):
        for name in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','DejaVuSans.ttf'):
            try:return ImageFont.truetype(name,size)
            except OSError:pass
        return ImageFont.load_default()
    draw.text((66,65),'RETRO REVERIE SOUNDS',font=font(27),fill='#ead9b9')
    # Artwork uses short preset labels; editable video title remains independent.
    draw.text((66,480),STYLE_NAMES[style],font=font(68),fill='#fff2da')
    draw.text((70,580),SCENE_NAMES[scene],font=font(38),fill='#ead9b9')
    tmp=episode/'thumbnail.partial.jpg';img.convert('RGB').save(tmp,format='JPEG',quality=90)
    os.replace(tmp,episode/'thumbnail.jpg')

def prepare(root, episode, edits=None):
    episode=Path(episode);root=Path(root)
    with open(episode/'.publication.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        settings=read(episode/'settings.json',{});state=read(episode/'status.json',{})
        if state.get('status')!='done':raise ValueError('Сначала дождись завершения выпуска')
        scene=settings.get('time_of_day','cycle');style=settings.get('style','morning')
        if scene not in SCENES or style not in STYLES:raise ValueError('Неизвестный пресет выпуска')
        existing=read(episode/'publication.json',{})
        if existing:
            title,description=existing['title'],existing['description']
        else:
            minutes=round(float(state.get('duration_seconds',settings.get('seconds',0)))/60,1)
            title=f'{SCENE_NAMES[scene]} | {STYLE_NAMES[style]} | {minutes:g} Minutes'
            tracks=read(episode/'tracklist.json',[])
            timeline=[]
            for i,t in enumerate(tracks):
                label=re.sub(r'[<>\r\n\x00]',' ',str(t.get('title',f'Track {i+1:02}')))
                timeline.append(f"{stamp(t['start_in_mix'])} {label}")
            description=(f'Welcome to Retro Reverie Sounds.\n\n'
                f'{STYLE_NAMES[style]} for quiet work, reading, or an unhurried break. '
                f'This session takes you to {SCENE_NAMES[scene].lower()}.\n\n'
                'Music created with AI tools and mixed for this session.\n\n'
                +('Tracklist (transitions overlap):\n'+'\n'.join(timeline)+'\n\n' if timeline else '')
                +'Slow moments. Timeless moods.\n\n#RetroReverieSounds #RelaxingMusic')
            if len(description)>5000:
                description=description[:4850].rsplit('\n',1)[0]+'\n\nFull tracklist is available in the episode files.'
        if edits is not None:title,description=edits['title'],edits['description']
        title,description=validate(title,description)
        if not (episode/'thumbnail.jpg').exists():artwork(root,episode,scene,style)
        data={'version':1,'title':title,'description':description,'thumbnail':'thumbnail.jpg'}
        atomic(episode/'title.txt',title+'\n');atomic(episode/'description.txt',description+'\n')
        atomic(episode/'publication.json',json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        return data
