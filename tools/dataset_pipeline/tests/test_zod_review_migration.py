import json

from bcdata.lane_boundary_completion import complete_lane_paths
from bcdata.zod_mirror import connect, encoded, sha
from bcdata.zod_review_sync import sync_reviews
from .test_lane_boundary_completion import geometry
from .test_zod_mirror import fixture, decision


def test_unchanged_reviews_survive_but_changed_targets_need_new_review(tmp_path):
    root, store = fixture(tmp_path, count=2)
    original = store._db()
    for index in range(2):
        g = geometry()
        if index == 0:
            g = complete_lane_paths(g)
        raw = encoded(g)
        original.execute('UPDATE frames SET geometry_json=?,geometry_sha=? WHERE image_id=?',
                         (raw, sha(raw.encode()), f'zod-{index:06}'))
    original.commit()
    original.close()
    for index in range(2):
        store.decide(decision(store.detail(f'zod-{index:06}'), event=f'migration-event-{index}'), 'Alice')
    original = store._db()
    original.execute('UPDATE zod_audit_members SET repair_required=1 WHERE image_id="zod-000000"')
    original.commit()
    prior = connect(root / 'repairs/continuous-boundaries-v4')
    original.backup(prior)
    new = connect(root / 'repairs/continuous-boundaries-v5')
    original.backup(new)
    original.close()
    events = [tuple(row) for row in prior.execute('SELECT * FROM audit_events ORDER BY event_id')]
    for row in new.execute('SELECT image_id,geometry_json FROM frames').fetchall():
        g = complete_lane_paths(json.loads(row['geometry_json']))
        g['version'] = 'new-version-metadata'
        raw = encoded(g)
        new.execute('UPDATE frames SET geometry_json=?,geometry_sha=? WHERE image_id=?',
                    (raw, sha(raw.encode()), row['image_id']))
    new.execute('UPDATE zod_audit_members SET repair_required=0')
    new.execute('DELETE FROM audit_events')
    new.commit()
    result = sync_reviews(root, new)
    assert result == {'reviews_carried': 1, 'changed_reviews_require_recheck': 1}
    assert new.execute('SELECT action FROM frames WHERE image_id="zod-000000"').fetchone()[0] == 'approve'
    assert new.execute('SELECT action FROM frames WHERE image_id="zod-000001"').fetchone()[0] is None
    assert new.execute('SELECT repair_required FROM zod_audit_members WHERE image_id="zod-000000"').fetchone()[0] == 1
    assert [tuple(row) for row in new.execute('SELECT * FROM audit_events ORDER BY event_id')] == events
    assert [tuple(row) for row in prior.execute('SELECT * FROM audit_events ORDER BY event_id')] == events
    assert prior.execute('SELECT COUNT(*) FROM frames WHERE action="approve"').fetchone()[0] == 2
    prior.close()
    new.close()
