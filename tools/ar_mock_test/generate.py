"""Generate a deterministic mock drive; no external footage or model inference."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT, FPS, DURATION = 960, 540, 30, 64
SPEED, FOV, MOUNT, HORIZON = 9.0, 60.0, 1.4, .68
FX = .5 / math.tan(math.radians(FOV / 2))
FY = FX * WIDTH / HEIGHT


def yaw_rate(t):
    if 7 <= t < 14:
        return -.035
    if 14 <= t < 21:
        return .035
    if 28 <= t < 34:
        return math.pi / 12
    return 0.0


def make_world():
    """A continuous road, extended beyond the video for the forward view."""
    dt = 1 / FPS
    points = [[0., 0., 0.]]  # east, north, course radians
    for i in range(int((DURATION + 20) * FPS)):
        x, z, yaw = points[-1]
        mid = yaw + yaw_rate(i * dt) * dt / 2
        points.append([x + SPEED * dt * math.sin(mid), z + SPEED * dt * math.cos(mid),
                       yaw + yaw_rate(i * dt) * dt])
    return np.array(points)


def coordinate(p):
    return {'latitude': 40 + p[1] / 111_320,
            'longitude': -74 + p[0] / (111_320 * math.cos(math.radians(40)))}


def projected_edges(world, index, half_width):
    pos = world[index]
    edges = []
    # Offset each world-space edge perpendicular to its local road tangent.
    # The two edges can have different depths on a curve; interpolate them
    # onto common image rows rather than pretending their depths are equal.
    for sign in [-1, 1]:
        edge=[]
        for p in world[index + 1:index + 270]:
            x=p[0]+sign*half_width*math.cos(p[2]);z=p[1]-sign*half_width*math.sin(p[2])
            dx,dz=x-pos[0],z-pos[1]
            forward=dx*math.sin(pos[2])+dz*math.cos(pos[2])
            lateral=dx*math.cos(pos[2])-dz*math.sin(pos[2])
            if forward<2.5:continue
            y=HORIZON-FY*MOUNT/forward;x=.5+FX*lateral/forward
            if .01<=y<=.66 and (not edge or y>edge[-1][0]+1e-6):edge.append([y,x])
        edges.append(np.array(edge))
    if any(len(e)<2 for e in edges):return []
    rows=[]
    for y in np.arange(.03,.641,.01):
        if any(not e[0,0]<=y<=e[-1,0] for e in edges):continue
        left,right=[float(np.interp(y,e[:,0],e[:,1])) for e in edges]
        left,right=max(0.,left),min(1.,right)
        if right-left>.041:rows.append([round(float(y),6),round(left,6),round(right,6)])
    return rows


def obstacle(world, index):
    point = world[min(len(world) - 1, index + 100)]
    pos = world[index]
    dx, dz = point[:2] - pos[:2]
    forward = dx * math.sin(pos[2]) + dz * math.cos(pos[2])
    lateral = dx * math.cos(pos[2]) - dz * math.sin(pos[2])
    if forward < 5:
        return []
    x = .5 + FX * lateral / forward
    y = HORIZON - FY * MOUNT / forward
    w, h = FX * 1.8 / forward, FY * 1.5 / forward
    return [[x - w / 2, y, w, h]] if 0 <= x - w / 2 < x + w / 2 <= 1 and 0 <= y < y + h <= 1 else []


def phase(t):
    if t < 7: return 'Straight road'
    if t < 14: return 'Left bend'
    if t < 21: return 'Right bend'
    if t < 24.2: return 'Approaching intersection'
    if t < 28: return 'Turn ahead: direction arrow'
    if t < 34: return '90-degree right turn: direction arrow'
    if t < 40: return 'Reacquire after turn'
    if t < 42: return 'Injected lane dropout'
    if t < 44: return 'Lane recovery'
    if t < 46: return 'Injected stale GPS'
    if t < 48: return 'GPS recovery'
    if t < 50: return 'Injected stale obstacles'
    if t < 52: return 'Injected inference overload'
    if t < 54: return 'Performance recovery'
    if t < 56: return 'Injected road dropout'
    if t < 58: return 'Road recovery'
    if t < 60: return 'Injected unverified mount'
    if t < 62: return 'Injected stale camera scene'
    return 'Final recovery'


def build(out):
    out.mkdir(parents=True, exist_ok=True)
    world = make_world()
    # One-meter-ish route points avoid an artificial dense-GPS branch ambiguity.
    route = [coordinate(p) for p in world[::4]]
    frames = []
    for n in range(DURATION * 10):
        t = n / 10
        index = n * 3
        gps_index = (index // 6) * 6  # distinct GPS captures at 5 Hz
        gps_time = gps_index / FPS
        if 44 <= t < 46:
            gps_time -= 3
            gps_index -= 90
        pos = world[gps_index]
        gps = {**coordinate(pos), 'capturedAt': round(gps_time, 6), 'speed': SPEED,
               'course': math.degrees(pos[2]) % 360, 'horizontalAccuracy': 3., 'courseAccuracy': 3.}
        frames.append({'time': t, 'phase': phase(t), 'gps': gps,
                       'lane': [] if 40 <= t < 42 else projected_edges(world, index, 1.8),
                       'road': [] if 54 <= t < 56 else projected_edges(world, index, 5.4),
                       'obstacles': obstacle(world, index),
                       'sceneAge': .5 if 60 <= t < 62 else 0., 'sourceAge': .5 if 60 <= t < 62 else 0.,
                       'mountVerified': not 58 <= t < 60,
                       'obstaclesFresh': not 48 <= t < 50,
                       'branchAmbiguous': 24.2 <= t < 34,
                       'vehicleLatency': .28 if 50 <= t < 52 else .045,
                       'laneLatency': .34 if 50 <= t < 52 else .06,
                       'roadLatency': .04, 'criticalHeat': False})
    fixture = {'schemaVersion': 1, 'width': WIDTH, 'height': HEIGHT, 'videoFPS': FPS,
               'duration': DURATION, 'horizontalFOV': FOV, 'mountHeight': MOUNT, 'horizon': HORIZON,
               'route': route, 'frames': frames}
    data = json.dumps(fixture, separators=(',', ':'), allow_nan=False).encode()
    (out / 'mock-drive.json').write_bytes(data)
    np.save(out / 'world.npy', world)
    start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    gpx = ET.Element('gpx', version='1.1', creator='BrakeCheck mock AR test', xmlns='http://www.topografix.com/GPX/1/1')
    track = ET.SubElement(gpx, 'trk'); ET.SubElement(track, 'name').text = 'SYNTHETIC AR test; not a real drive'
    seg = ET.SubElement(track, 'trkseg')
    for f in frames[::2]:
        gps = f['gps']
        pt = ET.SubElement(seg, 'trkpt', lat=str(gps['latitude']), lon=str(gps['longitude']))
        ET.SubElement(pt, 'time').text = (start + timedelta(seconds=gps['capturedAt'])).isoformat().replace('+00:00', 'Z')
    ET.ElementTree(gpx).write(out / 'mock-gps.gpx', encoding='utf-8', xml_declaration=True)
    (out / 'manifest.json').write_text(json.dumps({'synthetic': True, 'duration': DURATION, 'video_fps': FPS,
        'scenario_hz': 10, 'gps_hz': 5, 'fixture_sha256': hashlib.sha256(data).hexdigest(),
        'limitations': ['Video, GPS, lane/road observations and load events are synthetic.',
                        'Projection, map matching, admission and confidence use actual Swift app algorithms.',
                        'Does not measure detector accuracy, Core ML latency, thermal behavior or road safety.',
                        'JSON is the authoritative clock; GPX includes deliberate stale/out-of-order fixes.']}, indent=2) + '\n')
    print(f'Generated {len(frames)} scenario captures and {len(route)} route points in {out}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/ar-mock-test-20261023')
    build(parser.parse_args().out)
