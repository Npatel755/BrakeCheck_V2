"""Check actual gap survival, image rejection and the updated PC preview."""
import hashlib,json
from pathlib import Path
import cv2
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/ar-mock-test-20261023'
rows=json.loads((OUT/'swift-sustained-results.json').read_text())
fixture=json.loads((OUT/'mock-continuity-drive.json').read_text())
baseline=json.loads((OUT/'swift-gap-baseline-results.json').read_text())
assert len(rows)==len(fixture['frames'])==640
windows={}
for name,start,end,minimum in [('short lane',6,6.3,3),('short road',16,16.3,3),
    ('lane model gap',40,42,17),('overload',50,52,17),('road model gap',54,56,20)]:
    part=[r for r in rows if start<=r['time']<end]
    aligned=sum(r['confidence']=='aligned' for r in part)
    assert aligned>=minimum,(name,aligned)
    windows[name]={'seconds':end-start,'aligned_captures':aligned,'captures':len(part)}
for start,end in [(44,46),(48,50),(58,60),(60,62),(24.2,34)]:
    assert all(r['confidence']!='aligned' and not r['ribbon'] for r in rows if start<=r['time']<end)
for r,f in zip(rows,fixture['frames']):
    assert r['time']==f['time']
    assert r['confidence']=='aligned' or not r['ribbon']
    for group in r['ribbon']:
        for y,left,right in group:
            assert 0<=left<right<=1
            for x,by,w,h in f['obstacles']:
                assert not by-.01<=y<=by+h+.01 or right<x or left>x+w
cap=cv2.VideoCapture(str(OUT/'ar-sustained.mp4'));assert cap.isOpened()
assert int(cap.get(5))==30 and int(cap.get(7))==1920
decoded=0
while True:
    ok,image=cap.read()
    if not ok:break
    decoded+=1
cap.release();assert decoded==1920

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto((OUT/'sustained.html').as_uri())
    expect(page.locator('#status')).to_contain_text('64-second',timeout=20000)
    for t,mode in [(41.5,'ALIGNED'),(51.5,'ALIGNED'),(55.9,'ALIGNED'),(45,'LOST'),(49,'DIRECTIONAL')]:
        page.evaluate('(t)=>document.querySelector("video").currentTime=t',t)
        expect(page.locator('#time')).to_have_text(f'{t:.1f} s');expect(page.locator('#mode')).to_have_text(mode)
    page.evaluate('document.querySelector("video").currentTime=41.5')
    expect(page.locator('#time')).to_have_text('41.5 s')
    page.screenshot(path=str(OUT/'sustained-desktop.png'),full_page=True)
    assert not errors,errors;browser.close()
log=(ROOT/'test-results/ar-mock-20261023/sustained-core-tests.log').read_text()
assert "Executed 676 tests, with 0 failures" in log and "Test Suite 'All tests' passed" in log
files=['swift-sustained-results.json','ar-sustained.mp4','sustained.html']
sourceRoot=ROOT/'.worktrees/performance-main-v3/ios/Core'
sources=[sourceRoot/'Sources/BrakeCheckCore'/name for name in ['CameraARVisualContinuity.swift','CameraARScene.swift','NeuralLaneGeometry.swift','MockARReplay.swift']]
record={'passed':True,'core_tests_passed':676,'windows':windows,
    'baseline_aligned_captures':sum(r['confidence']=='aligned' for r in baseline),
    'updated_aligned_captures':sum(r['confidence']=='aligned' for r in rows),
    'conservative_rejections_seconds':[r['time'] for r in rows if 40<=r['time']<42 and r['confidence']!='aligned'],
    'camera_validation':'Bidirectional image matching, original-frame reacquisition and fresh-lane road-mask projection',
    'maximum_model_age_seconds':3,'installed_on_iphone':False,
    'artifact_sha256':{name:hashlib.sha256((OUT/name).read_bytes()).hexdigest() for name in files},
    'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
(OUT/'sustained-verification.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({k:v for k,v in record.items() if not k.endswith('sha256')},indent=2))
