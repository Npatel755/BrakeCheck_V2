"""Use decoded mock-video pixels, not idealized flow, for the continuity test."""
import base64
import hashlib
import json
from pathlib import Path
import cv2

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/ar-mock-test-20261023'
fixture=json.loads((OUT/'mock-drive.json').read_text())
capture=cv2.VideoCapture(str(OUT/'mock-road.mp4'))
assert capture.isOpened()
decoded=0
for f in fixture['frames']:
    target=round(f['time']*30)
    while decoded<=target:
        ok,picture=capture.read();assert ok
        decoded+=1
    small=cv2.resize(cv2.cvtColor(picture,cv2.COLOR_BGR2GRAY),(160,90),interpolation=cv2.INTER_AREA)
    f['cameraLuma']=base64.b64encode(small.tobytes()).decode()
    # Explicitly simulate inference misses while paint remains in the camera.
    # A negative observation is tested separately and must end continuity.
    if 6<=f['time']<6.3:f['lane']=[]
    if 16<=f['time']<16.3:f['road']=[]
    f['laneMissed']=not f['lane']
    f['roadMissed']=not f['road']
capture.release()
(OUT/'mock-continuity-drive.json').write_text(json.dumps(fixture,separators=(',',':')))
(OUT/'continuity-input.json').write_text(json.dumps({'camera_source':'mock-road.mp4',
    'camera_source_sha256':hashlib.sha256((OUT/'mock-road.mp4').read_bytes()).hexdigest(),
    'image_size':[160,90],'tracking':'Actual Swift LaneVisualTracker patch matching on decoded video pixels',
    'limitations':'Synthetic mock video and model misses; not real-road or iPhone validation.'},indent=2)+'\n')
print('Continuity fixture prepared from 640 decoded camera frames')
