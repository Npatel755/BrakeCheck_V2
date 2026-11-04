"""Assert synchronized artifacts and actual Swift fallback/projection behavior."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import cv2
from generate import ROOT, WIDTH, HEIGHT, FPS, DURATION


def verify(out):
    fixture = json.loads((out/'mock-drive.json').read_text())
    results = json.loads((out/'swift-results.json').read_text())
    assert len(results) == len(fixture['frames']) == DURATION*10
    assert all(r['time'] == f['time'] for r,f in zip(results,fixture['frames']))
    checks = []
    for start,end,level in [(24.2,34,'directional'),(40,42,'directional'),(44,46,'lost'),
                             (48,50,'directional'),(50,52,'lost'),(54,56,'directional'),
                             (58,60,'directional'),(60,62,'lost')]:
        rows=[r for r in results if start <= r['time'] < end]
        assert rows and all(r['confidence']==level and not r['ribbon'] for r in rows),(start,end)
        checks.append(f'{start:g}-{end:g}s: {level}, no aligned ribbon')
    for start,end in [(1,6),(8,13),(15,20),(35,39),(42.5,43.9),(46.5,47.9),(52.5,53.9),(56.5,57.9),(62.5,63.9)]:
        rows=[r for r in results if start <= r['time'] <= end]
        assert sum(r['confidence']=='aligned' for r in rows)/len(rows) >= .8,(start,end)
        checks.append(f'{start:g}-{end:g}s: alignment present on >=80% of mock captures')
    for r,f in zip(results,fixture['frames']):
        assert r['confidence']=='aligned' or not r['ribbon']
        for group in r['ribbon']:
            assert len(group)>=3
            for y,left,right in group:
                assert 0 <= left < right <= 1 and 0 <= y <= 1
                # Actual projection already clips to the road visibility mask.
                for x,by,w,h in f['obstacles']:
                    assert not by-.01 <= y <= by+h+.01 or right < x or left > x+w
    checks.append('No aligned geometry on downgraded frames; ribbon avoids every mock obstacle')
    video_checks={}
    for name in ['mock-road.mp4','ar-preview.mp4']:
        cap=cv2.VideoCapture(str(out/name));assert cap.isOpened()
        count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));fps=cap.get(cv2.CAP_PROP_FPS)
        assert count==DURATION*FPS and abs(fps-FPS)<.001
        assert (int(cap.get(3)),int(cap.get(4)))==(WIDTH,HEIGHT)
        # Decode all frames to catch truncated or broken export, not just metadata.
        decoded=0
        while True:
            ok,frame=cap.read()
            if not ok: break
            assert frame.shape==(HEIGHT,WIDTH,3);decoded+=1
        cap.release();assert decoded==count
        video_checks[name]={'frames_decoded':decoded,'fps':fps,'seconds':count/fps,'dimensions':[WIDTH,HEIGHT]}
    checks.append('Both videos fully decode: 1920 frames / 64 seconds, synchronized to 640 mock captures')
    manifest=json.loads((out/'manifest.json').read_text())
    for name,record in manifest['outputs'].items():
        path=out/name
        assert path.stat().st_size==record['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256'],name
    record={'passed':True,'checks':checks,'videos':video_checks,
            'modes':dict(Counter(r['confidence'] for r in results)),
            'limitations':manifest['limitations']}
    (out/'verification.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=ROOT/'reports/ar-mock-test-20261023');verify(p.parse_args().out)
