"""Read-only validation of published repairs and retained review evidence."""
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.zod_mirror import sha

ROOT = Path(__file__).resolve().parents[3]
root = ROOT / 'BrakeCheck_Dataset/experiments/zod_mirror'
db = sqlite3.connect((root / 'repairs/boundary-consolidation-v6/audits.sqlite').as_uri() + '?mode=ro', uri=True)
previous = sqlite3.connect((root / 'repairs/continuous-boundaries-v5/audits.sqlite').as_uri() + '?mode=ro', uri=True)
result = {'photos': 0, 'source_lanes': 0, 'model_proposals': 0, 'model_lanes': 0,
          'human_events_preserved': 0, 'source_assets_unchanged': True, 'fragmented_lanes': 0}


def validate(raw, digest, key):
    assert sha(raw.encode()) == digest
    geometry = json.loads(raw)
    for lane in geometry['lanes']:
        result[key] += 1
        assert len(lane['segments']) == 1
        pts = lane['points']
        rows = [p[1] for p in pts]
        assert rows == sorted(set(rows))
        assert set(range(math.ceil(rows[0]), math.floor(rows[-1]) + 1)).issubset(rows)
        assert all(math.isfinite(x) and math.isfinite(y) and 0 <= x < geometry['width'] and 0 <= y < geometry['height'] for x, y in pts)
        evidence = [point for section in lane.get('observed_paint_segments', lane['segments'])
                    for point in (section if isinstance(section, list) else section['points'])]
        assert rows[0] == min(p[1] for p in evidence) and rows[-1] == max(p[1] for p in evidence)


old_assets = {row[0]: row[1:] for row in previous.execute('SELECT image_id,native_sha,image_sha,image_path FROM frames')}
for image_id, raw, digest, native_sha, image_sha, image_path in db.execute('SELECT image_id,geometry_json,geometry_sha,native_sha,image_sha,image_path FROM frames'):
    assert old_assets[image_id] == (native_sha, image_sha, image_path)
    validate(raw, digest, 'source_lanes')
    result['photos'] += 1
for raw, digest in db.execute('SELECT geometry_json,geometry_sha FROM zod_model_proposals'):
    validate(raw, digest, 'model_lanes')
    result['model_proposals'] += 1
for event in previous.execute('SELECT * FROM audit_events'):
    assert db.execute('SELECT * FROM audit_events WHERE event_id=?', (event[0],)).fetchone() == event
    result['human_events_preserved'] += 1
result['samples'] = db.execute('SELECT COUNT(*) FROM zod_audit_members WHERE is_sample=1').fetchone()[0]
result['source_repair_findings'] = db.execute('SELECT COUNT(*) FROM zod_conversion_findings WHERE origin="zod-physical-boundaries-v6"').fetchone()[0]
for image_id, summary in db.execute('SELECT image_id,summary_json FROM zod_boundary_repair_current'):
    summary = json.loads(summary)
    row = db.execute('SELECT f.action,m.repair_required FROM frames f JOIN zod_audit_members m USING(image_id) WHERE image_id=?', (image_id,)).fetchone()
    assert row[1] == 1
    if summary['approved_requeued']:
        assert row[0] is None
assert result['photos'] == 100000 and result['model_proposals'] == 4840 and result['samples'] == 2420
(ROOT / 'reports/zod-import-20261023/boundary-v6-verification.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result))
