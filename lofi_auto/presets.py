"""UI and CLI share these musical and visual presets."""
STYLES = {
 'morning': {'label':'Morning jazz', 'bpm':75,'tested':True,'prompt':None},
 'lofi': {'label':'Lo-fi jazz', 'bpm':72,'tested':False,'prompt':'Instrumental lo-fi jazz. Warm Rhodes piano, mellow guitar accents, round bass and very quiet soft brushed drums. Gentle swing, connected melodic phrasing, subtle harmonic changes, smooth rounded attacks. No sharp percussion, no vocals.'},
 'chillout': {'label':'Coastal chillout', 'bpm':82,'tested':False,'prompt':'Instrumental coastal chillout. Airy electric piano, warm sustained synthesizer pads, delicate clean guitar and soft rounded bass. Restrained understated percussion, flowing melodic phrases, spacious ambience and gradual variation. No sharp accents, no vocals.'},
 'lounge': {'label':'Sunset lounge', 'bpm':85,'tested':False,'prompt':'Instrumental sunset lounge. Warm Rhodes chords, mellow jazz guitar, a lyrical understated piano melody, smooth bass and very quiet brushed percussion. Elegant relaxed harmony, gentle dynamic movement and connected phrases. No sharp accents, no vocals.'},
 'ambient': {'label':'Ambient piano', 'bpm':65,'tested':False,'prompt':'Instrumental ambient piano. Sparse expressive soft piano, slowly changing warm pads, low sustained bass and delicate room ambience. Long connected phrases with gentle evolving harmonies and rounded attacks. No drums, no percussion, no vocals.'},
}
MOODS = {'calm':{'label':'Спокойное','prompt':'Peaceful, unhurried mood.'},
 'warm':{'label':'Тёплое','prompt':'Warm comforting mood, gentle hopeful melodic answers.'},
 'dreamy':{'label':'Мечтательное','prompt':'Dreamy spacious mood, floating harmonies and delicate melodic variations.'},
 'reflective':{'label':'Задумчивое','prompt':'Reflective nostalgic mood, expressive restrained melody and tender harmonies.'}}
SCENES = {'cycle':{'label':'Смена суток','indices':[0,1,2,3,4]},
 'morning':{'label':'Дождливое утро','indices':[0]},
 'late_morning':{'label':'Позднее утро','indices':[1]},
 'day':{'label':'День','indices':[2]},
 'sunset':{'label':'Закат','indices':[3]},
 'evening':{'label':'Дождливый вечер','indices':[4]},
 'coast':{'label':'Тихое побережье','indices':[5]},
 'terrace':{'label':'Вечерняя терраса','indices':[6]},
 'lake':{'label':'Туманное озеро','indices':[7]}}
# Stable order: old episode indices retain their meaning when new assets are added.
BACKGROUNDS = ['01_early_morning.png','02_late_morning.png','03_lunch.png',
 '04_sunset.png','05_evening.png','06_coast.png','07_terrace.png','08_lake.png']
AUDITION_SCENES = {'chillout':'coast','lounge':'terrace','ambient':'lake'}
STYLE_DESCRIPTIONS = {
 'chillout':'82 BPM · воздушное электропиано, гитара, тёплые пэды и мягкий ритм.',
 'lounge':'85 BPM · Rhodes, джазовая гитара, плавный бас и тихие щётки.',
 'ambient':'65 BPM · редкое фортепиано и протяжённые пэды, без ударных.'}
for key, description in STYLE_DESCRIPTIONS.items():
 STYLES[key]['description'] = description
VARIATIONS=['Spacious melody with gentle answering phrases.', 'Small melodic variations and connected transitions.',
 'Tasteful changing harmonies and a gently moving bass line.', 'Sparse expressive phrases with sustained harmony.',
 'Delicate instrumental dialogue with subtle variations.', 'Natural flowing arrangement with soft dynamics.']

def templates(original, style='morning', mood='calm', bpm=None, custom_prompt=''):
    profile=STYLES[style]; result=[]
    for i,old in enumerate(original[:6]):
        j=dict(old)
        tempo=bpm or profile['bpm']
        if style=='morning':
            prompt=old['prompt']
            if bpm: prompt=prompt.replace('75 BPM',f'{bpm} BPM')
        else: prompt=f"{profile['prompt']} Tempo {tempo} BPM. {VARIATIONS[i]}"
        if mood!='calm' or style!='morning':prompt+=' '+MOODS[mood]['prompt']
        if custom_prompt.strip():prompt+=' '+custom_prompt.strip()
        j.update(prompt=prompt,bpm=tempo,style=style,mood=mood);result.append(j)
    return result
