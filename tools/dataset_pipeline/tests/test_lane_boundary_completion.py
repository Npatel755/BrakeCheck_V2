from copy import deepcopy
import json

import numpy as np
import pytest

from bcdata.lane_boundary_completion import POLICY, FULL_PATH_POLICY, complete_boundaries, complete_lane_paths


def test_continuous_lane_instance_fills_long_dash_gaps_without_extending_endpoints(tmp_path):
    from bcdata.converted_merge import _render
    from PIL import Image
    original = geometry()
    original['lanes'][0]['segments'][1] = [[310, 600], [311, 610]]
    result = complete_lane_paths(original)
    lane = result['lanes'][0]
    assert len(lane['segments']) == 1
    assert lane['observed_paint_segments'] == original['lanes'][0]['segments']
    assert [p[1] for p in lane['points']] == list(range(300, 611))
    assert complete_lane_paths(result) == result
    assert result['boundary_completion']['policy'] == FULL_PATH_POLICY
    _, _, _ = _render(np.zeros((720, 1280, 3), np.uint8), result, tmp_path, 'images/long-dash.jpg')
    mask = np.asarray(Image.open(tmp_path / 'laneseg_label_w16/images/long-dash.png'))
    assert all(np.any(mask[y] == 2) for y in range(247, 500))
    assert not np.any(mask[510:] == 2)


def test_uncertain_lane_order_is_flagged_instead_of_reintroducing_broken_targets():
    original = geometry()
    original['lanes'].append({'slot': 3, 'points': [[280, 385], [280, 400]],
                             'segments': [[[280, 385], [280, 400]]]})
    result = complete_lane_paths(original)
    assert result['boundary_completion']['requires_repair']
    assert result['boundary_completion']['association_issues']
    assert all(len(lane['segments']) == 1 for lane in result['lanes'])


def geometry():
    sections = [[[250 + .1 * y, y] for y in range(300, 371)],
                [[250 + .1 * y, y] for y in range(420, 501)]]
    return {"image_id": "test-dashes", "width": 1280, "height": 720,
            "lanes": [{"slot": 2, "physical": True, "points": sum(sections, []), "segments": sections}]}


def test_joins_dash_gap_and_preserves_source_endpoints_and_observations():
    original = geometry()
    saved = deepcopy(original)
    result = complete_boundaries(original)
    lane = result['lanes'][0]
    assert original == saved
    assert len(lane['segments']) == 1
    assert len(lane['points']) == 201
    assert lane['points'][0] == original['lanes'][0]['points'][0]
    assert lane['points'][-1] == original['lanes'][0]['points'][-1]
    assert lane['observed_paint_segments'] == original['lanes'][0]['segments']
    assert result['boundary_completion']['interpolated_gaps'] == 1
    assert lane['inferred_sections'][0]['observed_paint'] is False
    assert complete_boundaries(result) == result


def test_reported_tram_scene_small_polygon_tip_gaps_are_all_connected():
    from bcdata.zod_converter_v2 import convert_sections
    from pathlib import Path
    native = json.loads((Path(__file__).parent / 'fixtures/zod/zod-000001.json').read_text())
    source, _ = convert_sections(native, 'zod-000001')
    connected = complete_boundaries(source)
    assert connected['boundary_completion']['blocked_gaps'] == []
    assert [lane['slot'] for lane in connected['lanes']] == [2, 3]
    for lane in connected['lanes']:
        assert len(lane['segments']) == 1
        rows = [point[1] for point in lane['points']]
        assert all(b - a == 1 for a, b in zip(rows, rows[1:]))
        original = next(l for l in source['lanes'] if l['slot'] == lane['slot'])
        assert lane['observed_paint_segments'] == original['segments']


def test_reported_scene_export_has_a_target_on_every_interior_lane_row(tmp_path):
    from bcdata.zod_converter_v2 import convert_sections
    from bcdata.converted_merge import _render
    from pathlib import Path
    from PIL import Image
    native = json.loads((Path(__file__).parent / 'fixtures/zod/zod-000001.json').read_text())
    source, _ = convert_sections(native, 'zod-000001')
    connected = complete_boundaries(source)
    rgb = np.zeros((720, 1280, 3), np.uint8)
    cache, _, _ = _render(rgb, connected, tmp_path, 'images/tram.jpg')
    mask = np.asarray(Image.open(tmp_path / 'laneseg_label_w16/images/tram.png'))
    for lane in connected['lanes']:
        slot = lane['slot']
        first = min(p[1] for p in lane['points']) * 590 / 720
        last = max(p[1] for p in lane['points']) * 590 / 720
        assert all(point[0] >= 0 for point in cache[slot - 1] if first <= point[1] <= last)
        assert all(np.any(mask[y] == slot) for y in range(int(np.ceil(first)), int(np.floor(last)) + 1))


def test_small_vertical_gap_does_not_connect_distant_sections():
    source = geometry()
    source['lanes'][0]['segments'][1] = [[400, 375], [410, 390]]
    result = complete_boundaries(source)
    assert len(result['lanes'][0]['segments']) == 2
    assert result['boundary_completion']['blocked_gaps']


@pytest.mark.parametrize('reason', ['large', 'direction', 'occlusion'])
def test_unsupported_gaps_are_not_filled(reason):
    source = geometry()
    mask = None
    if reason == 'large':
        source['lanes'][0]['segments'][1] = [[310, 600], [311, 610]]
    elif reason == 'direction':
        source['lanes'][0]['segments'][1] = [[600, 420], [650, 500]]
    else:
        mask = np.zeros((720, 1280), np.float32)
        mask[390:410, 275:320] = 1
    result = complete_boundaries(source, mask)
    assert result['boundary_completion']['interpolated_gaps'] == 0
    assert len(result['lanes'][0]['segments']) == 2
    assert result['boundary_completion']['blocked_gaps']


def test_interpolated_path_cannot_cross_a_different_lane_boundary():
    source = geometry()
    source['lanes'].append({'slot': 3, 'points': [[280, 385], [280, 400]],
                            'segments': [[[280, 385], [280, 400]]]})
    result = complete_boundaries(source)
    assert result['boundary_completion']['interpolated_gaps'] == 0
    assert any(value['reason'] == 'boundary_order_conflict' for value in result['boundary_completion']['blocked_gaps'])


def test_actual_training_renderer_exports_connected_anchors_and_mask(tmp_path):
    from bcdata.converted_merge import _render
    from bcdata.export_ufld import ROWS_CACHE, NO_LANE
    from PIL import Image
    original = geometry()
    connected = complete_boundaries(original)
    rgb = np.zeros((720, 1280, 3), np.uint8)
    before, _, _ = _render(rgb, original, tmp_path, 'images/before.jpg')
    after, _, _ = _render(rgb, connected, tmp_path, 'images/after.jpg')
    # Canvas row 320 corresponds to source y=390.5, inside the dash gap.
    index = ROWS_CACHE.index(320)
    assert before[1][index][0] == NO_LANE
    assert after[1][index][0] > 0
    mask = np.asarray(Image.open(tmp_path / 'laneseg_label_w16/images/after.png'))
    assert mask[320, int(round(after[1][index][0]))] == 2
    # No extension past the original observed start/end.
    assert all(point[0] == NO_LANE for point in after[1] if point[1] > 500 * 590 / 720)
    assert connected['boundary_completion']['policy'] == POLICY


def test_review_confirmation_keeps_observed_and_interpolated_provenance(tmp_path):
    from .test_zod_mirror import fixture
    from bcdata.zod_mirror import connect, encoded, sha
    from bcdata.zod_main_review import detail, decide
    from bcdata.review_server import DecisionRequest
    root, store = fixture(tmp_path, count=1, warnings=True)
    db = connect(root)
    connected = complete_boundaries(geometry())
    connected.update(image_id='zod-000000', version='test-continuous-v3')
    raw = encoded(connected)
    db.execute('UPDATE frames SET geometry_json=?,geometry_sha=?', (raw, sha(raw.encode())))
    db.commit()
    db.close()
    item = detail(store, 'zod-000000')
    body = DecisionRequest(image_id=item['image_id'], revision=item['revision'], action='corrected',
                           event_id='continuous-review1', fingerprint=item['fingerprint'],
                           complete_annotation=True, lanes=connected['lanes'])
    decide(store, body, 'Nick')
    saved = detail(store, 'zod-000000')['geometry']
    assert saved['lanes'][0]['observed_paint_segments'] == connected['lanes'][0]['observed_paint_segments']
    assert saved['lanes'][0]['inferred_sections'] == connected['lanes'][0]['inferred_sections']
    assert saved['boundary_completion']['interpolated_gaps'] == 1


def test_continuous_batch_preserves_previous_votes_and_reuses_models_without_approving(tmp_path):
    from .test_zod_mirror import fixture, decision
    from bcdata.zod_mirror import connect, encoded, sha
    from bcdata.zod_continuous_batch import PREVIOUS, REPAIR, run
    from bcdata.zod_active_batch import active_batch
    root, store = fixture(tmp_path, count=1)
    store.decide(decision(store.detail('zod-000000')), 'Nick')
    source = connect(root)
    prior = connect(root / PREVIOUS)
    source.backup(prior)
    source.close()
    raw_geometry = geometry()
    raw_geometry.update(image_id='zod-000000', version='test-observed-v2')
    raw = encoded(raw_geometry)
    prior.execute('UPDATE frames SET geometry_json=?,geometry_sha=?', (raw, sha(raw.encode())))
    prior.execute('CREATE TABLE IF NOT EXISTS zod_model_proposals (image_id TEXT,model TEXT,version TEXT,geometry_json TEXT,geometry_sha TEXT,source_binding TEXT,PRIMARY KEY(image_id,model))')
    binding_row = prior.execute('SELECT native_sha,image_sha FROM frames').fetchone()
    binding = sha(encoded(list(binding_row)).encode())
    for name in ['first', 'second']:
        proposal = {**raw_geometry, 'model_label': name, 'teacher_version': name + ':v1'}
        data = encoded(proposal)
        prior.execute('INSERT INTO zod_model_proposals VALUES(?,?,?,?,?,?)', ('zod-000000', name, name + ':v1', data, sha(data.encode()), binding))
    prior.commit()
    before = dict(prior.execute('SELECT * FROM frames').fetchone())
    history = [tuple(row) for row in prior.execute('SELECT * FROM audit_events')]
    prior.close()
    (root / PREVIOUS / 'repair_status.json').write_text(json.dumps({'state': 'audit_ready'}))
    result = run(tmp_path)
    assert result['state'] == 'audit_ready' and result['interpolated_gaps'] == 1
    assert active_batch(root) == root / REPAIR
    db = connect(root / REPAIR)
    assert db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0] == 0
    assert db.execute('SELECT action FROM frames').fetchone()[0] is None
    assert db.execute('SELECT COUNT(*) FROM zod_model_proposals').fetchone()[0] == 2
    db.execute("UPDATE frames SET action='skip',reviewer='New reviewer',revision=12")
    db.commit()
    db.close()
    assert run(tmp_path) == result
    db = connect(root / REPAIR)
    assert tuple(db.execute('SELECT action,reviewer,revision FROM frames').fetchone()) == ('skip', 'New reviewer', 12)
    db.close()
    prior = connect(root / PREVIOUS)
    assert dict(prior.execute('SELECT * FROM frames').fetchone()) == before
    assert [tuple(row) for row in prior.execute('SELECT * FROM audit_events')] == history
    prior.close()
