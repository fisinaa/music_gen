from PIL import Image, ImageDraw, ImageFilter
import numpy as np
import subprocess, math, argparse
from pathlib import Path
from functools import lru_cache
from presets import BACKGROUNDS
P=Path(__file__).resolve().parent
W,H=1600,900;FPS=24
@lru_cache(maxsize=8)
def background(index):
 with Image.open(P/'scenes'/BACKGROUNDS[index]) as img:
  return img.convert('RGB').resize((W,H),Image.Resampling.LANCZOS)
mask=Image.new('L',(W,H));d=ImageDraw.Draw(mask)
d.polygon([(799,16),(1599,0),(1599,627),(800,493)],fill=255)
# Restrict left-pane animation to clear glass above the foreground plant.
d.polygon([(604,75),(743,32),(743,472),(636,448),(636,278),(604,254)],fill=255)
mask=mask.filter(ImageFilter.GaussianBlur(1)); ma=np.asarray(mask,dtype=np.float32)/255
rng=np.random.default_rng(33)
rains=[(rng.uniform(550,1650),rng.uniform(-100,900),rng.uniform(160,240),rng.uniform(9,20),int(rng.uniform(20,55))) for _ in range(95)]
drops=[(rng.uniform(835,1570),rng.uniform(-100,690),rng.uniform(12,31),rng.uniform(1.7,3.3),rng.uniform(0,7)) for _ in range(43)]
Y,X=np.mgrid[:H,:W].astype(np.float32)
# Coordinates are authored against the fixed 1600x900 backgrounds. Masks keep
# architecture, boats, trees and the foreground still; only water is displaced.
WATER_POLYGONS = {
 5:[(1157,326),(1599,312),(1599,438),(1510,455),(1440,513),(1420,558),
    (1356,590),(1280,590),(1230,574),(1130,553),(1070,532),(970,524),
    (894,494),(788,480),(790,469),(902,459),(1005,452),(1120,447),
    (1166,432),(1118,420),(1110,375)],
 6:[(1020,462),(1455,460),(1442,497),(1432,554),(1250,563),
    (1160,548),(1020,530)],
 7:[(353,535),(1585,535),(1599,890),(800,890),(737,842),(656,823),
    (603,797),(570,774),(560,738),(603,681),(591,628),(365,615)]}

@lru_cache(maxsize=3)
def outdoor_water(index):
 water=Image.new('L',(W,H));d=ImageDraw.Draw(water)
 d.polygon(WATER_POLYGONS[index],fill=255)
 if index==6:
  # Terrace handrail, vertical bars and both moored sailboats.
  d.line([(990,491),(1460,516)],fill=0,width=12)
  for x,y in [(1129,495),(1327,508)]:d.rectangle((x-7,y,x+7,580),fill=0)
  d.polygon([(1231,462),(1269,459),(1294,503),(1237,505)],fill=0)
  d.polygon([(1266,485),(1301,485),(1309,553),(1264,558)],fill=0)
 # Erode before feathering so animated edges stay inside the water.
 water=water.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.GaussianBlur(3))
 box=water.getbbox();base=np.asarray(background(index).crop(box),dtype=np.float32)
 alpha=np.asarray(water.crop(box),dtype=np.float32)[...,None]/255
 yy,xx=np.mgrid[:base.shape[0],:base.shape[1]].astype(np.float32)
 return box,base,alpha,xx,yy

def draw_outdoor(index,t):
 frame=background(index).copy();phase=math.tau*(t%12)/12
 box,base,alpha,xx,yy=outdoor_water(index)
 # Subpixel refraction, with smaller waves in the distance. Periodic at 12s.
 depth=np.clip(yy/max(1,base.shape[0]-1),0,1)
 strength=(.25+.75*depth)*(1.35 if index==5 else .7)
 dx=strength*(np.sin(yy*.085+phase*2)+.35*np.sin(xx*.028+yy*.055-phase))
 dy=strength*.35*np.sin(yy*.11+xx*.016-phase*2)
 sx=np.clip(xx+dx,0,base.shape[1]-1);sy=np.clip(yy+dy,0,base.shape[0]-1)
 x0=sx.astype(np.int32);y0=sy.astype(np.int32)
 x1=np.minimum(x0+1,base.shape[1]-1);y1=np.minimum(y0+1,base.shape[0]-1)
 fx=(sx-x0)[...,None];fy=(sy-y0)[...,None]
 moved=(base[y0,x0]*(1-fx)+base[y0,x1]*fx)*(1-fy)+(base[y1,x0]*(1-fx)+base[y1,x1]*fx)*fy
 # Modulate existing highlights, rather than painting unrelated white lines.
 shimmer=np.sin(yy*.17+np.sin(xx*.02)+phase*2)*np.sin(xx*.035-yy*.023-phase)
 moved*=1+shimmer[...,None]*(.035 if index==5 else .022)
 pixels=np.clip(np.rint(base*(1-alpha)+moved*alpha),0,255).astype('uint8')
 frame.paste(Image.fromarray(pixels),box[:2])
 if index==6:
  # Light stays anchored to the photographed candle flames, with a soft glow.
  glow=Image.new('RGBA',(W,H));gd=ImageDraw.Draw(glow)
  for j,(x,y,r) in enumerate([(401,471,12),(610,604,13),(724,596,12),(1347,650,18)]):
   flicker=.5+.3*math.sin(phase*3+j)+.2*math.sin(phase*7+j*1.8)
   gd.ellipse((x-r,y-r*1.7,x+r,y+r*1.7),fill=(255,182,77,int(18+35*flicker)))
  glow=glow.filter(ImageFilter.GaussianBlur(9))
  frame=Image.alpha_composite(frame.convert('RGBA'),glow).convert('RGB')
 if index==7:
  # Two overlapping banks of mist drift slowly over open water, below the shore.
  fog=(.12*np.exp(-((X-(1000+85*math.sin(phase)))/510)**2-((Y-(532+5*math.cos(phase)))/22)**2)
      +.065*np.exp(-((X-(740+65*math.cos(phase)))/370)**2-((Y-565)/29)**2))
  # Smooth horizontal gate excludes the foreground pine and pier.
  fog*=np.clip((X-390)/210,0,1)
  frame=Image.composite(Image.new('RGB',(W,H),(199,211,219)),frame,
      Image.fromarray(np.rint(fog*255).astype('uint8')))
 return frame

def draw_scene(index,t):
 if index >= 5:return draw_outdoor(index,t)
 frame=background(index).copy()
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
