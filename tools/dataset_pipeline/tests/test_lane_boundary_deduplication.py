from copy import deepcopy
import json
from pathlib import Path

import pytest

from bcdata.lane_boundary_deduplication import consolidate_boundaries


def scene(number):
    return json.loads((Path(__file__).parent / 'fixtures/zod' / f'overlap-{number:06}.json').read_text())


@pytest.mark.parametrize('number,slots', [(5033, [2]), (5909, [2]), (6567, [2, 3, 4])])
def test_reported_paired_stripes_become_one_main_boundary(number, slots):
    original = scene(number)
    before = deepcopy(original)
    fixed = consolidate_boundaries(original)
    assert [lane['slot'] for lane in fixed['lanes']] == slots
    main = next(lane for lane in fixed['lanes'] if lane['slot'] == 2)
    assert main['boundary_consolidation']['same_physical_boundary']
    assert len(main['segments']) == 1
    assert len(main['observed_paint_segments']) == sum(len(lane['observed_paint_segments']) for lane in original['lanes'] if lane['slot'] in (1, 2))
    assert original == before
    assert consolidate_boundaries(fixed) == fixed


def test_distinct_outer_lane_and_ambiguous_narrow_boundary_are_preserved():
    original = scene(4719)
    fixed = consolidate_boundaries(original)
    assert fixed['lanes'] == original['lanes']
    assert fixed['boundary_deduplication']['merged_pairs'] == []
    assert fixed['boundary_deduplication']['suspect_pairs'][0]['outer_slot'] == 4


def test_convergence_at_the_horizon_does_not_merge_distinct_lanes():
    def lane(slot, slope):
        pts = [[640 + slope * (y - 380), y] for y in range(380, 550)]
        return {'slot': slot, 'points': pts, 'segments': [pts]}
    g = {'width': 1280, 'height': 720, 'lanes': [lane(1, -2), lane(2, -.9), lane(3, 1), lane(4, 2.2)]}
    assert consolidate_boundaries(g) == g


def test_already_approved_bad_labels_are_fixed_and_require_a_new_check():
    from bcdata.zod_boundary_repair_batch import COLUMNS, prepare
    from bcdata.zod_mirror import encoded, sha
    g = scene(5033)
    raw = encoded(g)
    row = dict.fromkeys(COLUMNS)
    row.update(image_id=g['image_id'], geometry_json=raw, geometry_sha=sha(raw.encode()),
               qc_json='[]', revision=1, action='approve', reviewer='Alice', resolved_json='[]',
               native_sha='native', image_sha='image')
    before, fixed, report = prepare(tuple(row[key] for key in COLUMNS))
    assert before == row
    assert fixed['action'] is None and fixed['revision'] == 2
    assert report['approved_requeued']
    assert [lane['slot'] for lane in json.loads(fixed['geometry_json'])['lanes']] == [2]


def test_a_clean_human_correction_is_preserved_when_source_proposal_changes():
    from bcdata.zod_boundary_repair_batch import COLUMNS, prepare
    from bcdata.zod_mirror import encoded, sha
    g = scene(5033)
    correction = deepcopy(g)
    correction['lanes'] = [lane for lane in correction['lanes'] if lane['slot'] == 2]
    raw, human = encoded(g), encoded(correction)
    row = dict.fromkeys(COLUMNS)
    row.update(image_id=g['image_id'], geometry_json=raw, geometry_sha=sha(raw.encode()),
               correction_json=human, correction_sha=sha(human.encode()), qc_json='[]', revision=2,
               action='approve', reviewer='Alice', resolved_json='[]', native_sha='native', image_sha='image')
    _, fixed, report = prepare(tuple(row[key] for key in COLUMNS))
    assert fixed['action'] == 'approve' and fixed['correction_json'] == human
    assert not report['approved_requeued'] and not report['labels_changed']


def test_ambiguous_approved_boundaries_are_requeued_without_merging():
    from bcdata.zod_boundary_repair_batch import COLUMNS, prepare
    from bcdata.zod_mirror import encoded, sha
    g = scene(4719)
    raw = encoded(g)
    row = dict.fromkeys(COLUMNS)
    row.update(image_id=g['image_id'], geometry_json=raw, geometry_sha=sha(raw.encode()),
               qc_json='[]', revision=1, action='approve', reviewer='Alice', resolved_json='[]',
               native_sha='native', image_sha='image')
    before, fixed, report = prepare(tuple(row[key] for key in COLUMNS))
    assert before == row and report['approved_requeued'] and not report['labels_changed']
    assert fixed['action'] is None
    assert json.loads(fixed['geometry_json'])['lanes'] == g['lanes']
