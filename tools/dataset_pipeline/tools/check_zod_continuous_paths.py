"""Read-only verification of every v5 conversion and cached model proposal."""
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bcdata.zod_mirror import sha

ROOT = Path(__file__).resolve().parents[3]
batch = ROOT / 'BrakeCheck_Dataset/experiments/zod_mirror/repairs/continuous-boundaries-v5'
db = sqlite3.connect((batch / 'audits.sqlite').as_uri() + '?mode=ro', uri=True)
counts = {'photos': 0, 'converted_lanes': 0, 'model_proposals': 0, 'model_lanes': 0,
          'fragmented_lanes': 0, 'missing_interior_rows': 0, 'endpoint_extensions': 0}


def verify(raw, digest, lane_key):
    assert sha(raw.encode()) == digest
    geometry = json.loads(raw)
    for lane in geometry['lanes']:
        counts[lane_key] += 1
        assert len(lane['segments']) == 1
        assert lane['segments'][0] == lane['points']
        evidence = [point for section in lane['observed_paint_segments']
                    for point in (section if isinstance(section, list) else section['points'])]
        first, last = min(p[1] for p in evidence), max(p[1] for p in evidence)
        rows = [p[1] for p in lane['points']]
        assert rows == sorted(set(rows))
        assert rows[0] == first and rows[-1] == last
        assert set(range(math.ceil(first), math.floor(last) + 1)).issubset(rows)
        assert all(math.isfinite(x) and math.isfinite(y) and 0 <= x < geometry['width'] and
                   0 <= y < geometry['height'] for x, y in lane['points'])


for raw, digest in db.execute('SELECT geometry_json,geometry_sha FROM frames'):
    verify(raw, digest, 'converted_lanes')
    counts['photos'] += 1
for raw, digest in db.execute('SELECT geometry_json,geometry_sha FROM zod_model_proposals'):
    verify(raw, digest, 'model_lanes')
    counts['model_proposals'] += 1
counts['audit_samples'] = db.execute('SELECT COUNT(*) FROM zod_audit_members WHERE is_sample=1').fetchone()[0]
counts['association_findings_held'] = db.execute('SELECT COUNT(*) FROM zod_conversion_findings WHERE origin="zod-continuous-boundaries-v5"').fetchone()[0]
assert counts['photos'] == 100000 and counts['model_proposals'] == 4840 and counts['audit_samples'] == 2420
out = ROOT / 'reports/zod-import-20261023/continuous-v5-verification.json'
out.write_text(json.dumps(counts, indent=2), encoding='utf-8')
print(json.dumps(counts))
