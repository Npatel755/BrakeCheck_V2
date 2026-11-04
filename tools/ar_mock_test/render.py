"""Render mock-road and real-Swift-projection preview videos plus a local viewer."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import cv2
import numpy as np
from generate import ROOT, WIDTH, HEIGHT, FPS, DURATION, projected_edges, obstacle, phase

DEPS = ROOT / 'test-results/ar-mock-20261023/python-deps'
if DEPS.exists():
    sys.path.insert(0, str(DEPS))
import imageio_ffmpeg


def point(x, y):
    return int(round(x * WIDTH)), int(round((1 - y) * HEIGHT))


def polygon(rows):
    return np.array([point(r[1], r[0]) for r in rows] + [point(r[2], r[0]) for r in rows[::-1]], np.int32)


def line(img, rows, side, color, thickness):
    if len(rows) >= 2:
        cv2.polylines(img, [np.array([point(r[side], r[0]) for r in rows], np.int32)], False, color, thickness, cv2.LINE_AA)


def caption(img, text, position, size=.6, color=(240, 245, 250)):
    cv2.putText(img, text, position, cv2.FONT_HERSHEY_SIMPLEX, size, (10, 15, 20), 4, cv2.LINE_AA)
    cv2.putText(img, text, position, cv2.FONT_HERSHEY_SIMPLEX, size, color, 1, cv2.LINE_AA)


def scene(world, index, t):
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    horizon_px = round((1 - .68) * HEIGHT)
    for y in range(horizon_px):
        image[y, :] = (205 - int(y / 8), 172 + int(y / 12), 110 + int(y / 10))
    image[horizon_px:] = (57, 103, 73)
    # The lower foreground is pavement; the modeled visibility still only
    # supports the measured portion, as it does for an observed camera frame.
    cv2.fillPoly(image, [np.array([[0, HEIGHT], [WIDTH, HEIGHT], [WIDTH//2+50,horizon_px], [WIDTH//2-50,horizon_px]])], (68, 69, 71))
    road = projected_edges(world, index, 5.4)
    lane = projected_edges(world, index, 1.8)
    if road:
        cv2.fillPoly(image, [polygon(road)], (68, 69, 71))
        line(image, road, 1, (160, 170, 165), 2)
        line(image, road, 2, (160, 170, 165), 2)
    # Remove painted boundaries at the mock junction, preserving the curved
    # physical roadway. The route renderer correctly stays direction-only.
    if not 27 <= t < 34:
        line(image, lane, 1, (30, 215, 245), 3)
        line(image, lane, 2, (235, 235, 235), 3)
    # Simple roadside landmarks give the test a visible camera-motion cue.
    for k, row in enumerate(road[::18]):
        p = point(row[1] - .03, row[0]); q = point(row[2] + .03, row[0])
        h = int(12 + (1 - row[0] / .68) * 32)
        for x,y in [p,q]:
            cv2.line(image, (x,y), (x,y-h), (80, 80, 85), 2)
            cv2.circle(image, (x,y-h), max(3,h//5), (55, 125, 60), -1, cv2.LINE_AA)
    for x,y,w,h in obstacle(world, index):
        a,b = point(x,y+h),point(x+w,y)
        cv2.rectangle(image,a,b,(170,100,55),-1)
        cv2.rectangle(image,(a[0]+3,a[1]+3),(b[0]-3,a[1]+int((b[1]-a[1])*.4)),(70,70,65),-1)
        cv2.rectangle(image,(a[0],b[1]-8),(a[0]+5,b[1]-3),(35,35,230),-1)
        cv2.rectangle(image,(b[0]-5,b[1]-8),(b[0],b[1]-3),(35,35,230),-1)
    cv2.rectangle(image,(0,0),(WIDTH,77),(24,29,35),-1)
    caption(image,'SYNTHETIC VIDEO + GPS + PERCEPTION', (18,27), .62, (90,235,255))
    caption(image, f'{t:05.1f}s   {phase(t)}', (18,58), .58)
    return image


def overlay(base, result, frame):
    image = base.copy()
    mask = np.zeros((HEIGHT,WIDTH),np.uint8)
    for group in result['ribbon']:
        if len(group)>=3: cv2.fillPoly(mask,[polygon(group)],255)
    for x,y,w,h in frame['obstacles']:
        cv2.rectangle(mask,point(x-.012,y+h+.012),point(x+w+.012,y-.012),0,-1)
    tint = np.zeros_like(image);tint[:]=(235,210,35)
    selected = mask>0
    alpha=.5*result.get('opacity',1)
    image[selected]=(image[selected]*(1-alpha)+tint[selected]*alpha).astype(np.uint8)
    # Chevron accents remain clipped to the projected ribbon, rather than
    # extrapolating a turning path into unsupported pixels.
    accents=np.zeros_like(image)
    for group in result['ribbon']:
        for row in group[5::13]:
            y,left,right=row;cx,cy=point((left+right)/2,y);half=max(3,int((right-left)*WIDTH*.38))
            cv2.polylines(accents,[np.array([[cx-half,cy+6],[cx,cy-3],[cx+half,cy+6]],np.int32)],False,(255,255,240),2,cv2.LINE_AA)
    bright=(accents.max(axis=2)>0)&selected
    fade=result.get('opacity',1)
    image[bright]=(image[bright]*(1-fade)+accents[bright]*fade).astype(np.uint8)
    color={'aligned':(100,235,130),'directional':(80,200,255),'lost':(160,170,180)}[result['confidence']]
    cv2.rectangle(image,(0,HEIGHT-80),(WIDTH,HEIGHT),(24,29,35),-1)
    caption(image,f"AR: {result['confidence'].upper()}  |  {result['reason']}",(18,HEIGHT-51),.55,color)
    caption(image,f"Mock GPS {frame['gps']['latitude']:.6f}, {frame['gps']['longitude']:.6f}   20.1 mph   course {result['course']:.0f} deg",(18,HEIGHT-23),.49)
    if result['confidence']=='directional':
        cx,cy=WIDTH-70,125
        if frame['branchAmbiguous']:
            cv2.arrowedLine(image,(cx-30,cy+20),(cx-30,cy-15),(80,200,255),5,tipLength=.02)
            cv2.arrowedLine(image,(cx-30,cy-15),(cx+15,cy-15),(80,200,255),5,tipLength=.4)
        else: cv2.arrowedLine(image,(cx,cy+20),(cx,cy-25),(80,200,255),5,tipLength=.35)
    return image


def writer(path):
    return subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-y',
        '-f','rawvideo','-vcodec','rawvideo','-pix_fmt','bgr24','-s',f'{WIDTH}x{HEIGHT}','-r',str(FPS),'-i','-',
        '-an','-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(path)],stdin=subprocess.PIPE)


def html(out,fixture,results):
    compact=[{'t':r['time'],'phase':r['phase'],'mode':r['confidence'],'reason':r['reason'],'matched':r['mapMatched'],
              'progress':r['progressMeters'],'gps':f['gps']} for r,f in zip(results,fixture['frames'])]
    payload=json.dumps({'route':fixture['route'],'frames':compact},separators=(',',':')).replace('</','<\\/')
    template=(Path(__file__).parent/'viewer.html').read_text(encoding='utf-8')
    (out/'preview.html').write_text(template.replace('/*__PAYLOAD__*/',payload),encoding='utf-8')


def render(out):
    cv2.setNumThreads(2)
    fixture=json.loads((out/'mock-drive.json').read_text());results=json.loads((out/'swift-results.json').read_text())
    assert len(results)==len(fixture['frames'])==DURATION*10
    world=np.load(out/'world.npy')
    raw,preview=writer(out/'mock-road.mp4'),writer(out/'ar-preview.mp4')
    try:
        for index in range(DURATION*FPS):
            t=index/FPS;f=fixture['frames'][index//3];r=results[index//3]
            base=scene(world,index,t)
            raw.stdin.write(base.tobytes());preview.stdin.write(overlay(base,r,f).tobytes())
            if index%(FPS*8)==0:print(f'Rendered {t:.0f}/{DURATION}s',flush=True)
    finally:
        raw.stdin.close();preview.stdin.close()
        a,b=raw.wait(),preview.wait()
    assert a==b==0,(a,b)
    html(out,fixture,results)
    manifest=json.loads((out/'manifest.json').read_text())
    manifest['outputs']={name:{'bytes':(out/name).stat().st_size,'sha256':hashlib.sha256((out/name).read_bytes()).hexdigest()}
                         for name in ['mock-drive.json','mock-gps.gpx','mock-road.mp4','ar-preview.mp4','swift-results.json','preview.html']}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Videos and local viewer ready:',out/'preview.html')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=ROOT/'reports/ar-mock-test-20261023');render(p.parse_args().out)
