import json
from pathlib import Path
import time
import uuid

import numpy as np
import pytest
from PIL import Image

from bcdata import apply_backlog_recheck as installer
from bcdata.bootstrap import atomic_json
from bcdata.recheck_flagged_conversions import dependencies, snapshot, evaluate_one
from bcdata.review_store import ReviewStore
from bcdata.source_validation import refresh_groups
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture


def fixture(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    records = []
    directory = tmp_path / 'reports' / 'recheck'
    directory.mkdir()
    (directory / 'geometry').mkdir()
    with store.registry() as reg:
        prototype = dict(reg.image('sample-0'))
        for index in range(7):
            image_id = f'sample-{index}'
            if index >= 3:
                reg.add_image(image_id=image_id, source='comma10k', session_id=f'session-{index}',
                              source_ref=image_id, path=prototype['path'], width=1164, height=874,
                              sha256=str(index), license_id='comma10k', pool='commercial',
                              status='curated', split='train', tags={})
            reg.update_image(image_id, split='train', tags={'lighting': {'value': 'day'}})
            geometry = {'width': 1164, 'height': 874, 'image_id': image_id,
                        'version': 'empty-fixture', 'horizon_y': 367, 'valid_bottom': 665, 'lanes': []}
            geometry_path = settings.path('lane_geometry', image_id + '-empty.json')
            atomic_json(geometry_path, geometry)
            reg.add_annotation(image_id, 'geometry', 'geometry', 'empty-fixture', str(geometry_path))
            source_path = tmp_path / (image_id + '-source.png')
            Image.fromarray(np.ones((874, 1164), np.uint8)).save(source_path)
            reg.add_annotation(image_id, 'source_label', 'fixture', 'source-fixture', str(source_path),
                               params={'human': True, 'format': 'class_map'})
            reg.set_qc(image_id, 0.5, 'source_label', .5, [{'code': 'no_lanes'}], False, 'empty-fixture')
            teacher_path = settings.path('lane_geometry', 'teachers', 'comma_segnet_fresh', image_id + '.json')
            atomic_json(teacher_path, {'lanes': [{'slot': 2}]})
            geo = reg.annotation(image_id, 'geometry', 'geometry')
            source = reg.annotation(image_id, 'source_label')
            with reg.tx() as con:
                con.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (image_id, 'old', 'old-group', 'flagged', geo['id'], geo['model_version'], geo['path'],
                             file_sha256(geo['path']), source['id'], source['path'], file_sha256(source['path']),
                             json.dumps({'reasons': ['model_source_count_disagreement']}), time.time()))
            check = reg.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
            records.append(evaluate_one((snapshot(reg.conn, check, settings), str(directory / 'geometry'))))
    return settings, store, directory, records


def test_disagreement_gets_three_real_audits_and_no_automatic_approval(tmp_path, monkeypatch):
    settings, store, directory, records = fixture(tmp_path)
    assert all(not row['reasons'] and row['audit_risks'] for row in records)
    proof = {'code_sha256': dependencies()}
    atomic_json(directory / 'report.json', proof)
    (directory / 'past_review_controls.jsonl').write_text('')
    # Installation transaction tested separately from the production verifier's
    # requirement for 100+ real human controls; fixture reviews are never faked.
    monkeypatch.setattr(installer, 'load_verified', lambda *_: (proof, records))
    monkeypatch.setattr(installer, 'model_disagreement_cohorts', lambda rows, _: (rows, []))
    with store.registry() as reg:
        reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-6', 'active-phone', time.time() + 900))
        reg.conn.commit()
    result = installer.apply(settings, directory)
    assert result['counts'] == {'sample': 3, 'awaiting_audit': 3}
    assert result['groups'] == 1 and result['human_decisions_created'] == 0
    assert Path(result['backup'], 'metadata.sqlite').is_file()
    assert result['skipped'] == [{'image_id': 'sample-6', 'reason': 'reviewed_claimed_or_deferred'}]
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count('SELECT count(*) FROM reviews') == 0
        assert reg.count('SELECT count(*) FROM source_backlog_recheck_history') == 6
        assert reg.count("SELECT count(*) FROM images WHERE locked=1") == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
    refresh_groups(settings)
    assert store.stats()['source_validated'] == 0
    for image_id in result['sample_ids']:
        store.decide(image_id, 'approved', 'Fixture reviewer', store.detail(image_id)['revision'], str(uuid.uuid4()))
    refresh_groups(settings)
    assert store.stats()['source_validated'] == 3
    with pytest.raises(ValueError, match='already been applied'):
        installer.apply(settings, directory)


def test_changed_mask_and_saved_reviews_are_skipped(tmp_path, monkeypatch):
    settings, store, directory, records = fixture(tmp_path)
    proof = {'code_sha256': dependencies()}
    atomic_json(directory / 'report.json', proof)
    (directory / 'past_review_controls.jsonl').write_text('')
    monkeypatch.setattr(installer, 'load_verified', lambda *_: (proof, records))
    monkeypatch.setattr(installer, 'model_disagreement_cohorts', lambda rows, _: (rows, []))
    Image.fromarray(np.zeros((874, 1164), np.uint8)).save(records[0]['input']['source']['path'])
    store.decide('sample-1', 'approved', 'Fixture reviewer', 0, str(uuid.uuid4()))
    result = installer.apply(settings, directory)
    assert {item['reason'] for item in result['skipped']} == {'input_bytes_changed', 'reviewed_claimed_or_deferred'}
    assert store.detail('sample-1')['review_status'] == 'approved'
    assert len(result['sample_ids']) == 3


def test_failed_group_without_geometry_fix_cannot_be_resampled(tmp_path):
    settings, store, directory, records = fixture(tmp_path)
    record = records[0]['input']
    record['group'] = {'state': 'blocked'}
    result = evaluate_one((record, str(directory / 'geometry')))
    assert result['reasons'] == ['previous_audit_failed_requires_conversion_fix']


def test_past_frequent_corrections_keep_disagreements_manual(tmp_path):
    from copy import deepcopy
    settings, store, directory, records = fixture(tmp_path)
    controls = []
    for index in range(30):
        row = deepcopy(records[0])
        row['input']['image']['review_status'] = 'corrected' if index < 25 else 'approved'
        controls.append(row)
    adjusted, evidence = installer.model_disagreement_cohorts(records, controls)
    assert all(row['reasons'] == ['model_source_count_disagreement'] for row in adjusted)
    assert not evidence[0]['eligible_for_fresh_sample_audit']
    for row in controls:
        row['input']['image']['review_status'] = 'approved'
    adjusted, evidence = installer.model_disagreement_cohorts(records, controls)
    assert all(not row['reasons'] for row in adjusted)
    assert evidence[0]['eligible_for_fresh_sample_audit']
