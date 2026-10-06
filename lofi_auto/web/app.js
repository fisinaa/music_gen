'use strict';
const $=s=>document.querySelector(s), form=$('#create-form');
let state=null,music=[],selected=new Set(),musicLoaded=false,lastJobs='',lastEpisodes='',currentTab='studio',youtubeState=null,lastUploads='';
const statusNames={queued:'Ожидает',running:'В работе',succeeded:'Готово',failed:'Ошибка',interrupted:'Прервано',cancelled:'Отменено'};
const phases={generation:'Генерация музыки',audio:'Проверка и сведение',video:'Монтаж видео',complete:'Готово'};
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function notify(text){$('#notice').textContent=text;$('#notice').hidden=false;}
async function api(path,payload){const res=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(res.status===401)throw Error('Нужен вход. Обнови страницу и введи логин/пароль.');const data=await res.json();if(!res.ok)throw Error(data.error||'Ошибка запроса');return data;}
function tab(id){currentTab=id;document.querySelectorAll('.tab').forEach(n=>n.hidden=n.id!==id);document.querySelectorAll('nav button').forEach(n=>n.classList.toggle('active',n.dataset.tab===id));if(id==='music')loadMusic();if(id==='youtube')refreshYouTube();}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
function options(select,items,chosen){select.replaceChildren();Object.entries(items).forEach(([v,p])=>{const n=node('option',p.label);n.value=v;select.append(n);});select.value=chosen;}
function updateForm(){const isLibrary=form.elements.mode.value==='library',audio=form.elements.format.value==='audio';$('#library-pick').hidden=!isLibrary;$('#musical-card').style.opacity=isLibrary?'.5':'1';for(const key of ['style','mood','bpm','custom_prompt','track_min','track_max'])form.elements[key].disabled=isLibrary;form.elements.time_of_day.disabled=audio;form.elements.width.disabled=audio;if(state){const p=state.presets.styles[form.elements.style.value];$('#style-hint').textContent=p.tested?'Проверен на нашей системе.':'Экспериментальный пресет — первый результат стоит прослушать.';}}
for(const key of ['mode','format','style'])form.elements[key].onchange=updateForm;
$('#open-library').onclick=()=>tab('music');
function selectedCount(){$('#selection-count').textContent=`Выбрано: ${selected.size}`;$('#use-selection').textContent=`Использовать выбранные (${selected.size})`;}
$('#use-selection').onclick=()=>{form.elements.mode.value='library';updateForm();tab('studio');};
form.onsubmit=async e=>{e.preventDefault();$('#submit').disabled=true;try{const value=k=>form.elements[k].value;const p={title:value('title'),mode:value('mode'),minutes:Number(value('minutes')),style:value('style'),mood:value('mood'),time_of_day:value('time_of_day'),width:Number(value('width')),track_min:Number(value('track_min')),track_max:Number(value('track_max')),crossfade:Number(value('crossfade')),bpm:value('bpm')?Number(value('bpm')):null,custom_prompt:value('custom_prompt'),audio_only:value('format')==='audio',selected:[...selected]};await api('/api/jobs',p);notify('Выпуск добавлен в очередь. Браузер можно закрыть.');tab('queue');await refresh();}catch(e){notify(e.message);}finally{$('#submit').disabled=false;}};
$('#pause').onclick=async()=>{try{await api('/api/queue',{paused:!state.paused});await refresh();}catch(e){notify(e.message);}};
function button(text,fn,cls='secondary'){const b=node('button',text,cls);b.type='button';b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){notify(e.message);}finally{b.disabled=false;}};return b;}
function renderJobs(){const key=JSON.stringify(state.jobs);if(key===lastJobs)return;lastJobs=key;const opened=new Set([...document.querySelectorAll('#jobs details[open]')].map(n=>n.dataset.id));const parent=$('#jobs');parent.replaceChildren();if(!state.jobs.length){parent.append(node('div','Здесь появятся твои выпуски. Создай первый во вкладке «Создать выпуск».','empty'));return;}for(const j of state.jobs){const card=node('article',undefined,'card'),head=node('div',undefined,'job-head');head.append(node('div',j.title,'job-title'),node('span',statusNames[j.status]||j.status,'badge '+j.status));card.append(head);const p=j.payload;card.append(node('p',`${p.minutes} мин · ${state.presets.styles[p.style].label} · ${p.audio_only?'Только музыка':state.presets.scenes[p.time_of_day].label} · ${new Date(j.created*1000).toLocaleString()}`,'job-meta'));if(j.status==='running')card.append(node('p',phases[j.phase.stage]||'Подготовка задачи'));if(j.error)card.append(node('p',j.error,'hint'));if(j.log){const details=node('details'),summary=node('summary','Журнал');details.dataset.id=j.id;details.open=opened.has(j.id);details.append(summary,node('pre',j.log));card.append(details);}const actions=node('div',undefined,'job-actions');if(j.status==='queued')actions.append(button('Отменить',async()=>{await api('/api/job-action',{id:j.id,action:'cancel'});await refresh();}));if(['failed','interrupted'].includes(j.status))actions.append(button('Повторить',async()=>{if(!confirm('Перед повтором проверь, что старая генерация ACE-Step завершилась, либо перезапусти сервер ACE-Step. Подтверждаешь, что старой активной генерации нет?'))return;await api('/api/job-action',{id:j.id,action:'retry',confirmed:true});notify('Задача возвращена в очередь. Нажми «Продолжить очередь», когда будешь готов.');await refresh();}));if(j.status==='succeeded')actions.append(button('Открыть выпуски',()=>tab('episodes')));card.append(actions);parent.append(card);}}
async function loadMusic(){try{music=await api('/api/music');musicLoaded=true;const allowed=new Set(music.filter(t=>!t.excluded).map(t=>t.sha));selected=new Set([...selected].filter(id=>allowed.has(id)));renderMusic();}catch(e){notify(e.message);}}
function visibleMusic(){const q=$('#search').value.toLowerCase();return music.filter(t=>t.title.toLowerCase().includes(q)&&(!$('#favorites').checked||t.favorite));}
function trackDate(t){const d=new Date(t.created_at*1000);if(!Number.isFinite(d.getTime()))return 'Дата неизвестна';return (t.date_kind==='generation'?'Создан: ':'Дата файла: ')+new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}
function renderMusic(){const parent=$('#tracks');parent.replaceChildren();const rows=visibleMusic();if(!rows.length)parent.append(node('div','Треков пока нет. Сгенерируй выпуск или импортируй готовую очередь командой lofi collect.','empty'));for(const t of rows){const row=node('div',undefined,'track'+(t.excluded?' excluded':'')),check=node('input');check.type='checkbox';check.checked=selected.has(t.sha);check.disabled=t.excluded;check.onchange=()=>{if(check.checked)selected.add(t.sha);else selected.delete(t.sha);selectedCount();};const body=node('div');body.append(node('div',t.title,'track-title'),node('div',trackDate(t),'hint'),node('div',`${t.seconds?t.seconds+' сек · ':''}seed ${t.seed??'—'}`,'hint'));const audio=node('audio');audio.controls=true;audio.preload='none';audio.src='/media/music/'+t.sha;body.append(audio);const controls=node('div',undefined,'track-controls');const pref=async(favorite,excluded)=>{await api('/api/preference',{sha:t.sha,favorite,excluded});await loadMusic();};controls.append(button(t.favorite?'★':'☆',()=>pref(!t.favorite,t.excluded),'secondary star'+(t.favorite?' on':'')),button(t.excluded?'Вернуть':'Исключить',()=>pref(t.favorite,!t.excluded)));row.append(check,body,controls);parent.append(row);}selectedCount();}
$('#search').oninput=renderMusic;$('#favorites').onchange=renderMusic;$('#select-visible').onclick=()=>{visibleMusic().filter(t=>!t.excluded).forEach(t=>selected.add(t.sha));renderMusic();};$('#clear-selection').onclick=()=>{selected.clear();renderMusic();};
async function publicationEditor(card,e){
 const old=card.querySelector('.publication-editor');if(old){old.remove();return;}
 const data=await api('/api/publication',{name:e.name});
 const box=node('div',undefined,'publication-editor'),img=node('img');
 img.src='/media/episode/'+encodeURIComponent(e.name)+'/thumbnail.jpg';img.alt='Обложка выпуска';img.style.width='100%';box.append(img);
 const titleLabel=node('label','Название на английском'),title=node('input');title.value=data.title;title.maxLength=100;titleLabel.append(title);
 const descLabel=node('label','Описание и таймкоды'),desc=node('textarea');desc.value=data.description;desc.maxLength=5000;desc.rows=14;descLabel.append(desc);
 box.append(titleLabel,descLabel,node('p','Обложка использует стиль и сцену выпуска. Правка названия меняет текст публикации.','hint'));
 box.append(button('Сохранить оформление',async()=>{await api('/api/publication',{name:e.name,edits:{title:title.value,description:desc.value}});notify('Название и описание сохранены.');}));
 const downloads=node('div',undefined,'downloads');
 for(const [file,label] of [['thumbnail.jpg','Обложка'],['title.txt','Название'],['description.txt','Описание']]){const a=node('a',label);a.href='/media/episode/'+encodeURIComponent(e.name)+'/'+file;a.download=file;downloads.append(a);}
 box.append(downloads);
 if(e.video){
 const label=node('label','Этот выпуск предназначен специально для детей?'),kids=node('select');
 for(const [v,text] of [['','Выбери аудиторию'],['no','Нет'],['yes','Да']]){const o=node('option',text);o.value=v;kids.append(o);}label.append(kids);
 const synthLabel=node('label','Синтетический / AI-контент'),synth=node('input');synth.type='checkbox';synth.checked=true;synthLabel.append(synth);
 box.append(label,synthLabel,node('p','Загрузится приватное видео с этими текстами и обложкой. После добавления загрузки последующие правки оформления на неё не влияют.','hint'));
 box.append(button('Сохранить и загрузить приватно',async()=>{
 if(!kids.value)throw Error('Выбери аудиторию выпуска.');
 const yt=await api('/api/youtube');if(!yt.account.connected){tab('youtube');throw Error('Сначала подключи канал по инструкции во вкладке YouTube.');}
 const ch=yt.account.channel;if(!confirm(`Загрузить «${title.value}» приватным видео на канал ${ch.title} (${ch.id})?`))return;
 await api('/api/publication',{name:e.name,edits:{title:title.value,description:desc.value}});
 await api('/api/youtube/upload',{name:e.name,channel_id:ch.id,made_for_kids:kids.value==='yes',synthetic:synth.checked,confirmed:true});
 notify('Загрузка добавлена. Если этот MP4 уже отправлялся на канал, используется прежняя запись без дубля.');tab('youtube');await refreshYouTube();
 }));
 }
 card.append(box);
}
function renderEpisodes(){const key=JSON.stringify(state.episodes);if(key===lastEpisodes)return;lastEpisodes=key;const parent=$('#episode-list');parent.replaceChildren();if(!state.episodes.length){parent.append(node('div','Здесь будут готовые выпуски.','empty'));return;}for(const e of state.episodes){const card=node('article',undefined,'card'),base='/media/episode/'+encodeURIComponent(e.name)+'/';const player=node(e.video?'video':'audio');player.controls=true;player.preload='none';player.src=base+(e.video?'episode.mp4':'mix.wav');card.append(player,node('h2',e.title),node('p',`${e.seconds?Math.round(e.seconds/60*10)/10+' минут · ':''}${e.name}`,'hint'));const links=node('div',undefined,'downloads');for(const [file,title] of [[e.video?'episode.mp4':'mix.wav','Скачать'],['tracklist.txt','Треклист']]){const a=node('a',title);a.href=base+file;a.download=file;links.append(a);}card.append(links,button('Оформление выпуска',()=>publicationEditor(card,e)));parent.append(card);}}
async function refresh(){try{const first=!state;state=await api('/api/state');$('#connection').textContent='● подключено';$('#disk').textContent=`Свободно на диске: ${state.free_gb} GB`;$('#queue-count').textContent=state.jobs.filter(j=>['queued','running'].includes(j.status)).length;$('#pause').textContent=state.paused?'Продолжить очередь':'Пауза после выпуска';$('#queue-hint').textContent=state.paused?'Очередь на паузе. Текущий выпуск, если есть, завершится.':'Одновременно работает один выпуск. При ошибке очередь автоматически приостановится.';if(first){options($('#style'),state.presets.styles,'morning');options($('#mood'),state.presets.moods,'calm');options($('#time-of-day'),state.presets.scenes,'morning');updateForm();}renderJobs();renderEpisodes();}catch(e){$('#connection').textContent='● нет связи';if(!state)notify(e.message);}}
const uploadNames={queued:'В очереди',uploading:'Загрузка',initiating:'Создание сессии',thumbnail:'Загрузка обложки',done:'Передано YouTube',failed:'Ошибка',thumbnail_failed:'Видео загружено, ошибка обложки',needs_review:'Нужна проверка в YouTube Studio',cancelled:'Отменено'};
async function refreshYouTube(){try{
 youtubeState=await api('/api/youtube');const account=youtubeState.account;
 $('#youtube-account').textContent=account.connected?`${account.channel.title} · ${account.channel.id}`:'Канал ещё не подключён';
 const key=JSON.stringify(youtubeState.uploads);if(key===lastUploads)return;lastUploads=key;
 const parent=$('#youtube-uploads');parent.replaceChildren();if(!youtubeState.uploads.length)parent.append(node('p','Загрузок пока нет. Выбери готовый выпуск и открой его оформление.','hint'));
 for(const u of youtubeState.uploads){const card=node('article',undefined,'card');card.append(node('h2',u.episode),node('p',uploadNames[u.status]||u.status));
 const progress=node('progress');progress.max=u.size||1;progress.value=u.offset;progress.style.width='100%';card.append(progress,node('p',`${Math.floor(u.offset/Math.max(1,u.size)*100)}% · ${new Date(u.created*1000).toLocaleString()}`,'hint'));
 if(u.video_id){const link=node('a','Открыть видео в YouTube Studio');link.href='https://studio.youtube.com/video/'+encodeURIComponent(u.video_id)+'/edit';link.target='_blank';link.rel='noopener noreferrer';card.append(link,node('p','YouTube может ещё обрабатывать видео. Доступ при загрузке: приватный.','hint'));}
 if(u.error)card.append(node('p',u.error,'hint'));
 if(!u.has_repeat&&['failed','thumbnail_failed','cancelled'].includes(u.status))card.append(button(u.status==='thumbnail_failed'?'Повторить обложку':'Продолжить загрузку',async()=>{await api('/api/youtube/action',{id:u.id,action:'retry'});await refreshYouTube();}));
 if(u.repeat_of)card.append(node('p','Повторная загрузка выпуска','hint'));
 if(u.has_repeat)card.append(node('p','Для этой записи уже создана повторная загрузка — смотри более новую запись.','hint'));
 if(!u.has_repeat&&['done','failed','thumbnail_failed','needs_review','cancelled'].includes(u.status))card.append(button('Загрузить заново',async()=>{
 const fresh=await api('/api/youtube'),ch=fresh.account.channel;
 if(!fresh.account.connected||!ch||ch.id!==u.channel_id)throw Error('Подключи исходный канал этой загрузки.');
 if(!confirm(`Создать НОВОЕ приватное видео на канале ${ch.title} (${ch.id})? Будут использованы текущие сохранённые название, описание и обложка, а настройки аудитории и AI — из прежней загрузки. Старое видео останется на YouTube. Если прежний результат неизвестен, сначала проверь YouTube Studio.`))return;
 await api('/api/youtube/reupload',{id:u.id,channel_id:ch.id,confirmed:true});
 notify('Повторная загрузка добавлена. История прежней загрузки сохранена.');await refreshYouTube();
 }));
 if(u.status==='needs_review'){const link=node('a','Проверить список видео в YouTube Studio');link.href='https://studio.youtube.com/';link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
 parent.append(card);
 }
 }catch(e){$('#youtube-account').textContent='Не удалось получить состояние YouTube';if(currentTab==='youtube')notify(e.message);}}
async function poll(){await refresh();if(currentTab==='youtube')await refreshYouTube();setTimeout(poll,4000);}poll();
