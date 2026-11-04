import json

from bcdata.simulation_review import SimulationCorrection
from bcdata.zod_boundary_repair_batch import PREVIOUS, REPAIR, finalize, run
from bcdata.zod_mirror import connect, encoded, sha
from bcdata.zod_mirror_review import ZodMirrorReview
from .test_lane_boundary_deduplication import scene
from .test_zod_mirror import fixture, decision


def test_batch_preserves_approvals_and_in_flight_human_corrections(tmp_path):
    root, _ = fixture(tmp_path, count=2)
    base = connect(root)
    previous = connect(root / PREVIOUS)
    base.backup(previous)
    base.close()
    geometry = scene(5033)
    geometry['image_id'] = 'zod-000000'
    raw = encoded(geometry)
    previous.execute('UPDATE frames SET geometry_json=?,geometry_sha=? WHERE image_id=?', (raw, sha(raw.encode()), geometry['image_id']))
    previous.commit()
    previous.close()
    (root / PREVIOUS / 'repair_status.json').write_text(json.dumps({'state': 'audit_ready'}))
    live = ZodMirrorReview(tmp_path, batch=PREVIOUS)
    live.decide(decision(live.detail('zod-000000')), 'Alice')
    assert run(tmp_path)['state'] == 'staged'
    # Human saves made while staging must be retained by final activation.
    item = live.detail('zod-000000')
    lanes = [lane for lane in item['geometry']['lanes'] if lane['slot'] == 2]
    live.correct(SimulationCorrection(image_id=item['image_id'], revision=item['revision'], fingerprint=item['fingerprint'],
                 lanes=lanes, resolved_qc_reasons=[], approve=True), 'Alice')
    live.decide(decision(live.detail('zod-000001'), event='live-second-approval'), 'Bob')
    before = live.detail('zod-000000')
    result = finalize(tmp_path)
    assert result['state'] == 'audit_ready' and result['live_review_updates_preserved'] == 2
    repaired = ZodMirrorReview(tmp_path, batch=REPAIR)
    after = repaired.detail('zod-000000')
    assert after['geometry'] == before['geometry']
    assert after['decision'] == before['decision']
    assert repaired.detail('zod-000001')['decision']['reviewer'] == 'Bob'
    source, target = live._db(), repaired._db()
    for event in source.execute('SELECT * FROM audit_events'):
        assert tuple(target.execute('SELECT * FROM audit_events WHERE event_id=?', (event['event_id'],)).fetchone()) == tuple(event)
    assert run(tmp_path) == result
    source.close()
    target.close()
