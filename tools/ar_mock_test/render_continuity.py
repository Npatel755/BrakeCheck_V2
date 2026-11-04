"""Render baseline and bounded-continuity results against the same mock video."""
import json
from pathlib import Path
import cv2
from render import ROOT, FPS, DURATION, overlay, writer

OUT=ROOT/'reports/ar-mock-test-20261023'
fixture=json.loads((OUT/'mock-continuity-drive.json').read_text())
baseline=json.loads((OUT/'swift-gap-baseline-results.json').read_text())
continued=json.loads((OUT/'swift-continuity-results.json').read_text())
capture=cv2.VideoCapture(str(OUT/'mock-road.mp4'));assert capture.isOpened()
a,b=writer(OUT/'ar-gap-baseline.mp4'),writer(OUT/'ar-continuity.mp4')
try:
    for i in range(DURATION*FPS):
        ok,image=capture.read();assert ok
        f=fixture['frames'][i//3]
        a.stdin.write(overlay(image,baseline[i//3],f).tobytes())
        b.stdin.write(overlay(image,continued[i//3],f).tobytes())
        if i%(FPS*16)==0:print(f'Comparison rendered {i/FPS:.0f}/{DURATION}s',flush=True)
finally:
    a.stdin.close();b.stdin.close();capture.release()
    assert a.wait()==b.wait()==0

def compact(rows):
    return [{'t':r['time'],'phase':r['phase'],'mode':r['confidence'],'reason':r['reason'],
             'matched':r['mapMatched'],'progress':r['progressMeters'],'gps':f['gps'],
             'tracking':r['continuity'],'opacity':r['opacity']} for r,f in zip(rows,fixture['frames'])]
payload=json.dumps({'route':fixture['route'],'frames':compact(continued),
    'variants':{'baseline':compact(baseline),'continuity':compact(continued)}},separators=(',',':')).replace('</','<\\/')
template=(Path(__file__).parent/'viewer.html').read_text(encoding='utf-8')
template=template.replace('src="ar-preview.mp4"','src="ar-continuity.mp4"')
template=template.replace('href="mock-drive.json"','href="mock-continuity-drive.json"')
template=template.replace('href="verification.json"','href="continuity-verification.json"')
template=template.replace('beside ar-preview.mp4','beside ar-continuity.mp4 and ar-gap-baseline.mp4')
template=template.replace('Mock drive with synchronized GPS','Brief-gap continuity comparison')
template=template.replace('Watch the real Swift AR projection and fallback rules over a generated road. Video, GPS, perception and overload events are simulated.',
    'Compare the same mock drive with and without bounded visual continuity. Short misses can fade through; long gaps still remove the ribbon. This prototype is not installed on the iPhone.')
template=template.replace('<div class="chapters" id="chapters"></div>',
    '<p><label>Comparison: <select id="variant"><option value="continuity">Gap-tolerant prototype</option><option value="baseline">Baseline</option></select></label></p><div class="chapters" id="chapters"></div>')
template=template.replace("const chapters=[[0,'Straight'],", "const chapters=[[0,'Straight'],[6,'Short lane miss'],[16,'Short road miss'],")
template=template.replace('f=data.frames[Math.min(data.frames.length-1,Math.floor((t+1e-6)*10))]',
    'rows=data.variants[el("variant").value],f=rows[Math.min(rows.length-1,Math.floor((t+1e-6)*10))]')
template=template.replace("set('reason',f.reason);", "set('reason',f.reason+(f.tracking?' · opacity '+Math.round(f.opacity*100)+'%':''));")
template=template.replace('v.addEventListener(\'error\',', '''el('variant').addEventListener('change',()=>{
const time=v.currentTime,playing=!v.paused;v.pause();v.src=el('variant').value==='baseline'?'ar-gap-baseline.mp4':'ar-continuity.mp4';
v.addEventListener('loadedmetadata',()=>{v.currentTime=time;if(playing)v.play();update();},{once:true});v.load();
});
v.addEventListener('error',''')
(OUT/'continuity.html').write_text(template.replace('/*__PAYLOAD__*/',payload),encoding='utf-8')
print('Comparison viewer ready:',OUT/'continuity.html')
