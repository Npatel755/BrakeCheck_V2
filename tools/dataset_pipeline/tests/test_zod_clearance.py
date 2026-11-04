import json

from bcdata.simulation_review import SimulationCorrection, SimulationReview
from bcdata.zod_mirror_review import ZodCorrection
from bcdata.zod_audit_groups import record_finding, states
from bcdata.zod_clearance import clearance_counts, repartition
from .test_zod_mirror import decision, fixture


def confirm(store, image_id, event, lanes=None):
    item = store.detail(image_id)
    original = SimulationReview._validate_lanes(item['geometry']['lanes'], 1280, 720, source_metadata=True)
    return store.correct(ZodCorrection(image_id=image_id, revision=item['revision'],
        fingerprint=item['fingerprint'], event_id=event, lanes=original if lanes is None else lanes,
        resolved_qc_reasons=item['source_qc_reasons'], approve=True, complete_annotation=True,
        no_visible_lane_paint=not (original if lanes is None else lanes)), 'Human')


def test_unchanged_whole_photo_confirmations_pass_source_audit(tmp_path):
    _, store = fixture(tmp_path, warnings=True)
    for index in (0, 2, 4):
        confirm(store, f'zod-{index:06}', f'confirmation-{index:08}')
    counts = store.status()['counts']
    assert counts['groups_audited'] == 1
    assert counts['label_cleared'] == 5
    assert counts['individually_approved'] == 3
    assert counts['source_audit_cleared'] == 2


def test_changed_labels_still_hold_peers_but_not_saved_human_labels(tmp_path):
    _, store = fixture(tmp_path, warnings=True)
    confirm(store, 'zod-000000', 'correction-00001', lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    counts = store.status()['counts']
    assert counts['groups_blocked'] == 1
    assert counts['label_cleared'] == 1
    assert counts['source_audit_cleared'] == 0


def test_migration_preserves_reviews_and_separates_machine_findings(tmp_path):
    _, store = fixture(tmp_path, count=8, warnings=True)
    confirm(store, 'zod-000000', 'confirmation-00001')
    db = store._db()
    record_finding(db, 'zod-000001', 'boundary_order_conflict', 'converter')
    before = [tuple(r) for r in db.execute('SELECT * FROM audit_events')]
    frames = [tuple(r) for r in db.execute('SELECT * FROM frames')]
    receipt = repartition(db)
    assert receipt['human_decisions_created'] == 0
    assert receipt['previous_samples_preserved'] == 3
    assert [tuple(r) for r in db.execute('SELECT * FROM frames')] == frames
    assert [tuple(r) for r in db.execute('SELECT * FROM audit_events')] == before
    groups = states(db)
    assert sorted(v['state'] for v in groups.values()) == ['awaiting_audit', 'needs_repair']
    assert db.execute('SELECT SUM(is_sample) FROM zod_audit_members').fetchone()[0] == 4
    assert db.execute('SELECT COUNT(*) FROM zod_audit_members_before_cause_v2').fetchone()[0] == 8
    assert repartition(db) == receipt
    db.close()
    assert store.detail('zod-000000')['decision']['reviewer'] == 'Human'


def test_old_confirmation_bug_is_repaired_but_real_edits_remain_held(tmp_path):
    _, store = fixture(tmp_path, count=5, warnings=True)
    confirm(store, 'zod-000000', 'confirmation-00001')
    confirm(store, 'zod-000002', 'correction-00001', lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    db = store._db()
    db.execute("UPDATE zod_audit_members SET repair_required=1 WHERE image_id='zod-000000'")
    db.commit()
    receipt = repartition(db)
    assert receipt['mistaken_confirmation_flags_corrected'] == 1
    assert db.execute("SELECT repair_required FROM zod_audit_members WHERE image_id='zod-000000'").fetchone()[0] == 0
    assert db.execute("SELECT repair_required FROM zod_audit_members WHERE image_id='zod-000002'").fetchone()[0] == 1
    assert clearance_counts(db, states(db))['label_cleared'] == 2
    db.close()


def test_missing_native_annotations_cannot_be_cleared_for_peers_by_empty_confirmation(tmp_path):
    _, store = fixture(tmp_path, count=1)
    db = store._db()
    row = db.execute('SELECT geometry_json FROM frames').fetchone()
    from bcdata.zod_mirror import encoded, sha
    geometry = json.loads(row[0]); geometry['lanes'] = []
    raw = encoded(geometry)
    db.execute('UPDATE frames SET geometry_json=?,geometry_sha=?,qc_json=?',
               (raw, sha(raw.encode()), '["missing_or_invalid_native_lane_annotations"]'))
    db.execute('DELETE FROM zod_audit_plan')
    db.commit(); db.close()
    confirm(store, 'zod-000000', 'empty-confirmation-001', lanes=[])
    counts = store.status()['counts']
    assert counts['groups_blocked'] == 1
    assert counts['individually_approved'] == 1
    assert counts['source_audit_cleared'] == 0


def test_completed_machine_repair_gets_fresh_audit_gate_and_undo_keeps_gate(tmp_path):
    from .test_lane_boundary_deduplication import scene
    from bcdata.lane_boundary_deduplication import consolidate_boundaries
    from bcdata.zod_mirror import encoded, sha
    from bcdata.zod_review_undo import undo
    _, store = fixture(tmp_path, count=1)
    db = store._db()
    geometry = consolidate_boundaries(scene(5033))
    geometry['image_id'] = 'zod-000000'
    raw = encoded(geometry)
    db.execute('UPDATE frames SET geometry_json=?,geometry_sha=?,qc_json=?',
               (raw, sha(raw.encode()), '["paired_paint_boundary_consolidated_requires_review"]'))
    db.execute('DELETE FROM zod_audit_plan'); db.commit()
    from bcdata.zod_audit_groups import ensure_plan
    ensure_plan(db)
    record_finding(db, 'zod-000000', 'Outer/main boundary consolidation', 'converter')
    db.execute('CREATE TABLE zod_boundary_repair_current(image_id TEXT,source_revision INTEGER,summary_json TEXT)')
    db.execute('INSERT INTO zod_boundary_repair_current VALUES(?,?,?)',
               ('zod-000000', 0, '{"source_merged_pairs":1}'))
    db.commit()
    receipt = repartition(db)
    assert receipt['repaired_sources_moved_to_sample_gates'] == 1
    assert next(iter(states(db).values()))['state'] == 'awaiting_audit'
    assert clearance_counts(db, states(db))['label_cleared'] == 0
    db.close()
    confirm(store, 'zod-000000', 'confirm-repaired-source')
    assert store.status()['counts']['label_cleared'] == 1
    undo(store, 'confirm-repaired-source', 'Human')
    assert store.status()['counts']['pending'] == 1
    assert store.status()['counts']['groups_blocked'] == 0
    # New diagnoses are independent; an old repair gate cannot suppress them.
    confirm(store, 'zod-000000', 'confirm-repaired-source-again')
    db = store._db()
    record_finding(db, 'zod-000000', 'New unresolved source error', 'later-inspection')
    db.close()
    undo(store, 'confirm-repaired-source-again', 'Human')
    assert store.status()['counts']['groups_blocked'] == 1


def test_old_true_failure_is_not_erased_by_later_unchanged_confirmation(tmp_path):
    _, store = fixture(tmp_path, count=5, warnings=True)
    original = SimulationReview._validate_lanes(store.detail('zod-000000')['source_geometry']['lanes'],
                                                1280, 720, source_metadata=True)
    confirm(store, 'zod-000000', 'actual-edit-0001', lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    confirm(store, 'zod-000000', 'revert-source-0001', lanes=original)
    db = store._db()
    assert repartition(db)['mistaken_confirmation_flags_corrected'] == 0
    assert next(iter(states(db).values()))['state'] == 'needs_repair'
    db.close()


def test_new_model_comparisons_keep_continuous_boundary_geometry_without_changing_input():
    from copy import deepcopy
    from pathlib import Path
    from bcdata.zod_model_proposals import shape_for_batch
    geometry = {'width': 1280, 'height': 720, 'lanes': [
        {'slot': 2, 'points': [[400, 400], [410, 420], [440, 480], [450, 500]],
         'segments': [[[400, 400], [410, 420]], [[440, 480], [450, 500]]]}]}
    before = deepcopy(geometry)
    fixed, version = shape_for_batch(geometry, 'model-v1', Path('boundary-consolidation-v6'))
    assert geometry == before
    assert version == 'model-v1:continuous-v5'
    assert len(fixed['lanes'][0]['segments']) == 1
    assert fixed['lanes'][0]['points'][0] == [400, 400]
    assert fixed['lanes'][0]['points'][-1] == [450, 500]
