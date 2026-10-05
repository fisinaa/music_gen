from PIL import Image, ImageDraw, ImageFilter
import numpy as np
import subprocess, math, argparse
from pathlib import Path
P=Path(__file__).resolve().parent
W,H=1600,900;FPS=24
bases=[Image.open(f).convert('RGB').resize((W,H),Image.Resampling.LANCZOS) for f in sorted((P/'scenes').glob('*.png'))]
assert len(bases)==5,'Expected 5 scene backgrounds'
mask=Image.new('L',(W,H));d=ImageDraw.Draw(mask)
d.polygon([(799,16),(1599,0),(1599,627),(800,493)],fill=255)
# Restrict left-pane animation to clear glass above the foreground plant.
d.polygon([(604,75),(743,32),(743,472),(636,448),(636,278),(604,254)],fill=255)
mask=mask.filter(ImageFilter.GaussianBlur(1)); ma=np.asarray(mask,dtype=np.float32)/255
rng=np.random.default_rng(33)
rains=[(rng.uniform(550,1650),rng.uniform(-100,900),rng.uniform(160,240),rng.uniform(9,20),int(rng.uniform(20,55))) for _ in range(95)]
drops=[(rng.uniform(835,1570),rng.uniform(-100,690),rng.uniform(12,31),rng.uniform(1.7,3.3),rng.uniform(0,7)) for _ in range(43)]
Y,X=np.mgrid[:H,:W].astype(np.float32)
def draw_scene(index,t):
 frame=bases[index].copy()
 fog=(.075*np.exp(-((X-(1000+t*8))/330)**2-((Y-(345+12*np.sin(t*.3)))/108)**2)+.032*np.exp(-((X-(1490-t*7))/280)**2-((Y-460)/70)**2))*ma
 frame=Image.composite(Image.new('RGB',(W,H),(183,201,210)),frame,Image.fromarray((fog*255*(1 if index in (0,4) else 0)).astype('uint8')))
 layer=Image.new('RGBA',(W,H));ld=ImageDraw.Draw(layer)
 for x,y,v,l,o in (rains if index in (0,4) else []):
  yy=(y+t*v)%850-60; xx=x-yy*.055
  ld.line([(xx,yy),(xx-1.5,yy+l)],fill=(205,223,234,o),width=1)
 for x,y,v,r,phase in (drops if index in (0,4) else []):
  yy=(y+v*t*0.67+4*math.sin(t*.9*0.67+phase))%820-90
  xx=x+2*math.sin(yy*.025+phase)
  # Slow rivulets: a faint narrow trail and refractive dark edge/light glint.
  trail=[(xx+1.4*math.sin(k*.14+phase),yy-k) for k in range(0,31,2)]
  ld.line(trail,fill=(206,222,232,42),width=2)
  ld.ellipse((xx-r-1,yy-r*1.7,xx+r+1,yy+r*1.8),fill=(30,48,58,65))
  ld.ellipse((xx-r,yy-r*1.6,xx+r,yy+r*1.5),fill=(181,211,227,65))
  ld.arc((xx-r,yy-r*1.6,xx+r,yy+r*1.5),25,145,fill=(241,245,248,170),width=1)
  ld.ellipse((xx-r*.6,yy-r*.9,xx-r*.1,yy-r*.25),fill=(234,241,246,135))
 layer.putalpha(Image.fromarray((np.asarray(layer.getchannel('A'),dtype=np.float32)*ma).astype('uint8')))
 frame=Image.alpha_composite(frame.convert('RGBA'),layer)
 # Steam wisps: an upward-travelling curl whose opacity vanishes with height.
 steam=Image.new('RGBA',(W,H));sd=ImageDraw.Draw(steam)
 for j in range(0 if index==2 else 3):
  points=[]
  for k in range(105):
   u=k/104
   yy=(543 if index==3 else 524)-130*u
   xx=703+(j-1)*15+(5+13*u)*math.sin(u*6.7-t*1.25+j*1.7)+9*u*math.sin(t*.45+j)
   points.append((xx,yy))
  for k in range(104):
   u=k/104
   strength=(math.sin(math.pi*u)**.7)*(.65+.35*math.sin(u*11-t*2+j)**2)
   sd.line([points[k],points[k+1]],fill=(230,229,218,int(150*strength)),width=7+int(8*u))
 steam=steam.filter(ImageFilter.GaussianBlur(6.0))
 frame=Image.alpha_composite(frame,steam).convert('RGB')

 return frame
