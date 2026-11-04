"""Render sustained tracking output; this changes behavior, not playback timing."""
import json
from pathlib import Path
import cv2
from render import ROOT, FPS, DURATION, overlay, writer

OUT=ROOT/'reports/ar-mock-test-20261023'
fixture=json.loads((OUT/'mock-continuity-drive.json').read_text())
results=json.loads((OUT/'swift-sustained-results.json').read_text())
capture=cv2.VideoCapture(str(OUT/'mock-road.mp4'));assert capture.isOpened()
video=writer(OUT/'ar-sustained.mp4')
try:
    for index in range(DURATION*FPS):
        ok,image=capture.read();assert ok
        video.stdin.write(overlay(image,results[index//3],fixture['frames'][index//3]).tobytes())
        if index%(FPS*16)==0:print(f'Tracking preview {index/FPS:.0f}/{DURATION}s',flush=True)
finally:
    video.stdin.close();capture.release();assert video.wait()==0
compact=[{'t':r['time'],'phase':r['phase'],'mode':r['confidence'],'reason':r['reason'],
          'matched':r['mapMatched'],'progress':r['progressMeters'],'gps':f['gps']}
         for r,f in zip(results,fixture['frames'])]
payload=json.dumps({'route':fixture['route'],'frames':compact},separators=(',',':'))
template=(Path(__file__).parent/'viewer.html').read_text(encoding='utf-8')
template=template.replace('src="ar-preview.mp4"','src="ar-sustained.mp4"')
template=template.replace('href="mock-drive.json"','href="mock-continuity-drive.json"')
template=template.replace('href="verification.json"','href="sustained-verification.json"')
template=template.replace('beside ar-preview.mp4','beside ar-sustained.mp4')
template=template.replace('Mock drive with synchronized GPS','AR tracking through model gaps')
template=template.replace('Watch the real Swift AR projection and fallback rules over a generated road. Video, GPS, perception and overload events are simulated.',
    'Successive camera frames maintain road alignment when model updates are missing. This is the updated tracking algorithm on the mock drive, with normal playback speed. Not installed on the iPhone yet.')
(OUT/'sustained.html').write_text(template.replace('/*__PAYLOAD__*/',payload),encoding='utf-8')
print('Updated tracking preview ready:',OUT/'sustained.html')
