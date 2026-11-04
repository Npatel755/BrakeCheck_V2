"""Verify bounded gap handling against the same-input conservative baseline."""
import hashlib
import json
from pathlib import Path
import cv2
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/ar-mock-test-20261023'
baseline=json.loads((OUT/'swift-gap-baseline-results.json').read_text())
continued=json.loads((OUT/'swift-continuity-results.json').read_text())
fixture=json.loads((OUT/'mock-continuity-drive.json').read_text())
assert len(baseline)==len(continued)==len(fixture['frames'])==640
checks=[]
for start,end in [(6,6.3),(16,16.3),(40,40.7),(54,54.7)]:
    rows=[(a,b) for a,b in zip(baseline,continued) if start<=a['time']<end]
    assert rows and all(a['confidence']!='aligned' and b['confidence']=='aligned' and b['continuity'] for a,b in rows)
    assert all(.25<=b['opacity']<1 for _,b in rows)
    assert all(a['time']==b['time'] for a,b in rows)
    checks.append(f'{start:g}-{end:g}s: baseline has no aligned ribbon; tracked prototype fades through')
for start,end,level in [(40.7,42,'directional'),(44,46,'lost'),(48,50,'directional'),(50,52,'lost'),
                         (54.7,56,'directional'),(58,60,'directional'),(60,62,'lost'),(24.2,34,'directional')]:
    rows=[r for r in continued if start<=r['time']<end]
    assert rows and all(r['confidence']==level and not r['ribbon'] for r in rows),(start,end)
checks.append('Long gaps expire; stale GPS/obstacles/camera, overload, mount and intersection still downgrade')
for a,b,f in zip(baseline,continued,fixture['frames']):
    assert a['time']==b['time']==f['time']
    assert b['confidence']=='aligned' or not b['ribbon']
    for group in b['ribbon']:
        for y,left,right in group:
            assert 0<=left<right<=1
            for x,by,w,h in f['obstacles']:
                assert not by-.01<=y<=by+h+.01 or right<x or left>x+w
for name in ['ar-gap-baseline.mp4','ar-continuity.mp4']:
    cap=cv2.VideoCapture(str(OUT/name));assert cap.isOpened()
    assert int(cap.get(5))==30 and int(cap.get(7))==1920
    decoded=0
    while True:
        ok,image=cap.read()
        if not ok:break
        decoded+=1
    cap.release();assert decoded==1920
checks.append('Both comparison videos fully decode and share the same 64-second timeline')

with sync_playwright() as tool:
    browser=tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        headless=True,args=['--mute-audio'])
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto((OUT/'continuity.html').as_uri())
    expect(page.locator('#status')).to_contain_text('64-second',timeout=20000)
    page.evaluate('document.querySelector("video").currentTime=6.1')
    expect(page.locator('#time')).to_have_text('6.1 s');expect(page.locator('#mode')).to_have_text('ALIGNED')
    expect(page.locator('#reason')).to_contain_text('fading')
    page.select_option('#variant','baseline')
    expect(page.locator('#time')).to_have_text('6.1 s');expect(page.locator('#mode')).to_have_text('DIRECTIONAL')
    page.select_option('#variant','continuity')
    expect(page.locator('#time')).to_have_text('6.1 s');expect(page.locator('#mode')).to_have_text('ALIGNED')
    for time,mode in [(40.4,'ALIGNED'),(41,'DIRECTIONAL'),(45,'LOST'),(49,'DIRECTIONAL'),(61,'LOST')]:
        page.evaluate('(t)=>document.querySelector("video").currentTime=t',time)
        expect(page.locator('#time')).to_have_text(f'{time:.1f} s');expect(page.locator('#mode')).to_have_text(mode)
    page.evaluate('document.querySelector("video").currentTime=40.4')
    expect(page.locator('#time')).to_have_text('40.4 s')
    page.screenshot(path=str(OUT/'continuity-desktop.png'),full_page=True)
    page.set_viewport_size({'width':420,'height':1000})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors,errors
    browser.close()
checks.append('Local viewer switches baseline/prototype at the same timestamp and reports the correct state')
log=(ROOT/'test-results/ar-mock-20261023/continuity-core-tests.log').read_text()
assert "Executed 673 tests, with 0 failures" in log and "Test Suite 'All tests' passed" in log
sources=[ROOT/'.worktrees/performance-main-v3/ios/Core/Sources/BrakeCheckCore/CameraARVisualContinuity.swift',
         ROOT/'.worktrees/performance-main-v3/ios/Core/Sources/BrakeCheckCore/MockARReplay.swift']
record={'passed':True,'core_tests_passed':673,'checks':checks,
    'baseline_aligned_captures':sum(r['confidence']=='aligned' for r in baseline),
    'continuity_aligned_captures':sum(r['confidence']=='aligned' for r in continued),
    'additional_aligned_mock_seconds':sum(a['confidence']!='aligned' and b['confidence']=='aligned' for a,b in zip(baseline,continued))/10,
    'camera_tracking':'Actual LaneVisualTracker patch matches on decoded synthetic video',
    'maximum_original_observation_age_seconds':.75,'installed_on_iphone':False,
    'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
    'artifact_sha256':{name:hashlib.sha256((OUT/name).read_bytes()).hexdigest() for name in [
        'mock-continuity-drive.json','swift-gap-baseline-results.json','swift-continuity-results.json','ar-gap-baseline.mp4','ar-continuity.mp4','continuity.html']}}
(OUT/'continuity-verification.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps({k:v for k,v in record.items() if not k.endswith('sha256')},indent=2))
