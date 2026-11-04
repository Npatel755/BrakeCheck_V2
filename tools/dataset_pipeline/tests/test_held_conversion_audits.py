"""Existing held conversions need real sampled decisions, never invented votes."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
import uuid

import numpy as np
from PIL import Image
import pytest

from bcdata import held_conversion_audits as audit
from bcdata.review_store import ReviewStore
from bcdata.source_validation import refresh_groups
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture


def make_held(tmp_path, definitions=None):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    definitions = definitions or [{'image_id': f'sample-{index}', 'session_id': f'session-{index}'}
                                  for index in range(3)]
    source_path = tmp_path / 'original-human-source.png'
    Image.fromarray(np.zeros((874, 1164), np.uint8)).save(source_path)
    with store.registry() as reg:
        template = dict(reg.image('sample-0'))
        geometry_template = json.loads(Path(reg.annotation('sample-0', 'geometry', 'geometry')['path']).read_text())
        for definition in definitions:
            image_id = definition['image_id']
            if reg.image(image_id) is None:
                image = {**template, 'image_id': image_id, 'sha256': image_id,
                         'source_ref': 'fixture:' + image_id}
                reg.add_image(**image)
            reg.update_image(image_id, source=definition.get('source', 'a2d2'),
                             session_id=definition.get('session_id', image_id),
                             split=definition.get('split', 'train'),
                             tags={'lighting': {'value': definition.get('lighting', 'day')}})
            geometry = deepcopy(geometry_template)
            geometry['image_id'] = image_id
            if definition.get('lanes', 1) == 0:
                geometry['lanes'] = []
            elif definition.get('lanes') == 2:
                lane = deepcopy(geometry['lanes'][0])
                lane['id'], lane['slot'], lane['x_ref'] = 1, 3, 800
                lane['points'] = [[800, 650, True], [650, 410, True]]
                lane['fit'] = {'basis': 'interp', 'ys': [410, 650], 'xs': [650, 800]}
                geometry['lanes'].append(lane)
            geometry_path = tmp_path / 'lane_geometry' / (image_id + '.json')
            geometry_path.write_text(json.dumps(geometry), encoding='utf-8')
            reg.add_annotation(image_id, 'geometry', 'geometry', 'fixture-auto', str(geometry_path))
            reg.set_qc(image_id, .2, 'low', .8,
                       [{'code': reason} for reason in definition.get('reasons', ['low_mask_coverage'])],
                       False, 'fixture-auto')
            params = {'human': True, 'format': 'class_map'}
            if definition.get('palette'):
                palette = tmp_path / (image_id + '-palette.json')
                palette.write_text('{"lane":2}')
                params.update(palette_path=str(palette), palette_sha256=file_sha256(palette))
            reg.add_annotation(image_id, 'source_label', 'fixture', 'source-human-v1', str(source_path), params=params)
            geom = dict(reg.annotation(image_id, 'geometry', 'geometry'))
            source = dict(reg.annotation(image_id, 'source_label'))
            key = 'old-' + image_id
            reg.conn.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                             (key, 'old-batch', 'old-stratum', '[]', definition.get('group_state', 'awaiting_audit'),
                              '{"original":true}', time.time()))
            reg.conn.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (image_id, 'old-batch', key, 'flagged', geom['id'], geom['model_version'], geom['path'],
                              file_sha256(geom['path']), source['id'], source['path'], file_sha256(source['path']),
                              json.dumps({'reasons': definition.get('reasons', ['low_mask_coverage']),
                                          'original_evidence': 'preserve'}), time.time()))
            reg.conn.commit()
    return settings, store


def snapshot(store):
    with store.registry() as reg:
        return {table: [dict(row) for row in reg.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                for table in ('images', 'annotations', 'qc', 'source_conversion_checks',
                              'source_validation_groups', 'reviews', 'review_events', 'review_versions')}


def new_groups(store):
    with store.registry() as reg:
        return [dict(row) for row in reg.conn.execute("SELECT * FROM source_validation_groups WHERE batch_id!='old-batch'")]


def test_planning_is_read_only_and_applying_only_creates_sampled_holds(tmp_path):
    settings, store = make_held(tmp_path)
    before = snapshot(store)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    assert snapshot(store) == before
    receipt = audit.apply(settings, directory)
    after = snapshot(store)
    assert after['images'] == before['images']
    assert after['annotations'] == before['annotations']
    assert after['qc'] == before['qc']
    assert after['reviews'] == after['review_events'] == []
    assert len(after['source_validation_groups']) == len(before['source_validation_groups']) + 1
    assert len(new_groups(store)) == 1
    group = new_groups(store)[0]
    assert group['state'] == 'awaiting_audit'
    assert len(json.loads(group['sample_ids'])) == 3
    assert {row['state'] for row in after['source_conversion_checks']} == {'sample'}
    assert receipt['human_decisions_created'] == 0 and receipt['training_held']
    backup = Path(receipt['backup'])
    assert (backup / 'metadata.sqlite').is_file() and (backup / 'human_labels.zip').is_file()
    for old, current in zip(before['source_conversion_checks'], after['source_conversion_checks']):
        for name in ('geometry_id', 'geometry_version', 'geometry_path', 'geometry_sha256',
                     'source_id', 'source_path', 'source_sha256'):
            assert current[name] == old[name]
        details = json.loads(current['diagnostics'])
        assert details['reasons'] == json.loads(old['diagnostics'])['reasons']
        assert details['original_evidence'] == 'preserve'


def test_groups_never_mix_source_split_lighting_or_primary_failure_family(tmp_path):
    definitions = [
        {'image_id': 'same-a', 'reasons': ['low_mask_coverage', 'slot_conflict']},
        {'image_id': 'same-b', 'reasons': ['slot_conflict', 'low_mask_coverage']},
        {'image_id': 'other-reasons', 'reasons': ['low_mask_coverage']},
        {'image_id': 'other-source', 'source': 'comma10k', 'reasons': ['low_mask_coverage', 'slot_conflict']},
        {'image_id': 'other-split', 'split': 'val', 'reasons': ['low_mask_coverage', 'slot_conflict']},
        {'image_id': 'other-light', 'lighting': 'night', 'reasons': ['low_mask_coverage', 'slot_conflict']},
        {'image_id': 'same-family-other-pattern', 'lanes': 2, 'reasons': ['crossing']},
    ]
    settings, store = make_held(tmp_path, definitions)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    groups = new_groups(store)
    assert len(groups) == 5
    with store.registry() as reg:
        for group in groups:
            members = reg.conn.execute('SELECT image_id FROM source_conversion_checks WHERE group_key=?', (group['group_key'],)).fetchall()
            selected = json.loads(group['sample_ids'])
            assert len(selected) == len(members)
            if len(members) == 3:
                assert set(selected) == {'same-a', 'same-b', 'same-family-other-pattern'}


def test_hundred_photo_cap_and_three_real_session_balanced_samples(tmp_path):
    definitions = [{'image_id': f'photo-{index:03}', 'session_id': f'session-{index % 7}'} for index in range(105)]
    settings, store = make_held(tmp_path, definitions)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    groups = new_groups(store)
    assert len(groups) == 2
    with store.registry() as reg:
        members = []
        for group in groups:
            count = reg.count('SELECT count(*) FROM source_conversion_checks WHERE group_key=?', (group['group_key'],))
            assert 1 <= count <= 100
            samples = json.loads(group['sample_ids'])
            assert len(samples) == min(3, count)
            sessions = {reg.image(image_id)['session_id'] for image_id in samples}
            all_sessions = {row[0] for row in reg.conn.execute('SELECT DISTINCT i.session_id FROM source_conversion_checks c JOIN images i USING(image_id) WHERE c.group_key=?', (group['group_key'],))}
            assert len(sessions) == min(3, len(all_sessions))
            members.append(count)
        assert sum(members) == 105
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='sample'") == 6
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='awaiting_audit'") == 99


def test_small_groups_audit_every_member(tmp_path):
    settings, store = make_held(tmp_path, [{'image_id': 'one'}, {'image_id': 'two'}])
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    assert len(new_groups(store)) == 1
    assert set(json.loads(new_groups(store)[0]['sample_ids'])) == {'one', 'two'}


def test_actual_sample_approvals_release_only_waiting_members(tmp_path):
    settings, store = make_held(tmp_path, [{'image_id': f'photo-{index}'} for index in range(5)])
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    samples = json.loads(new_groups(store)[0]['sample_ids'])
    for image_id in samples[:2]:
        store.decide(image_id, 'approved', 'Nick', store.detail(image_id)['revision'], str(uuid.uuid4()))
    refresh_groups(settings)
    assert new_groups(store)[0]['state'] == 'awaiting_audit'
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
    image_id = samples[2]
    store.decide(image_id, 'approved', 'Nick', store.detail(image_id)['revision'], str(uuid.uuid4()))
    refresh_groups(settings)
    assert new_groups(store)[0]['state'] == 'passed'
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 2
        assert reg.count('SELECT count(*) FROM reviews') == 3
        assert reg.count('SELECT count(*) FROM review_events') == 3
        assert reg.count("SELECT count(*) FROM images WHERE locked=0") >= 2


@pytest.mark.parametrize('action', ['corrected', 'rejected'])
def test_corrected_or_rejected_sample_blocks_cohort_without_resetting_prior_failure(tmp_path, action):
    settings, store = make_held(tmp_path, [{'image_id': f'photo-{index}'} for index in range(5)])
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    image_id = json.loads(new_groups(store)[0]['sample_ids'])[0]
    store.decide(image_id, action, 'Nick', store.detail(image_id)['revision'], str(uuid.uuid4()),
                 **({'lanes': []} if action == 'corrected' else {}))
    refresh_groups(settings)
    assert new_groups(store)[0]['state'] == 'blocked'
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0


@pytest.mark.parametrize('protection', ['claim', 'deferral', 'human', 'history', 'blocked', 'stale_geometry', 'stale_source'])
def test_planning_preserves_prior_reviews_failed_groups_claims_deferrals_and_stale_bindings(tmp_path, protection):
    settings, store = make_held(tmp_path)
    with store.registry() as reg:
        if protection == 'claim':
            reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-0', 'owner', time.time() + 600))
        elif protection == 'deferral':
            reg.conn.execute('INSERT INTO review_deferrals VALUES (?,?,?)', ('sample-0', 'Nick', time.time()))
        elif protection == 'human':
            reg.add_annotation('sample-0', 'human', 'Nick', 'human', reg.annotation('sample-0', 'geometry')['path'])
        elif protection == 'history':
            reg.add_review('sample-0', 'Nick', 'approved')
            reg.update_image('sample-0', review_status='pending', locked=0)
        elif protection == 'blocked':
            reg.conn.execute("UPDATE source_validation_groups SET state='blocked' WHERE group_key='old-sample-0'")
        else:
            kind = 'geometry' if protection == 'stale_geometry' else 'source_label'
            old = reg.annotation('sample-0', kind)
            reg.add_annotation('sample-0', kind, 'geometry' if kind == 'geometry' else 'fixture',
                               'moved-binding', old['path'], params=json.loads(old['params'] or '{}'))
        reg.conn.commit()
        before = dict(reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone())
    directory = tmp_path / 'audit-plan'
    plan = audit.prepare(settings, directory)
    assert 'sample-0' not in {row['image_id'] for row in plan['members']}
    audit.apply(settings, directory)
    with store.registry() as reg:
        after = dict(reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone())
        assert before == after
        if protection == 'blocked':
            assert reg.conn.execute("SELECT state FROM source_validation_groups WHERE group_key='old-sample-0'").fetchone()[0] == 'blocked'


@pytest.mark.parametrize('change', ['claim', 'deferral', 'human', 'review', 'blocked', 'new_geometry', 'new_source',
                                  'source_bytes', 'geometry_bytes', 'palette_bytes', 'config'])
def test_changed_frozen_inputs_abort_the_whole_batch_without_partial_groups(tmp_path, change):
    settings, store = make_held(tmp_path, [{'image_id': f'sample-{index}', 'palette': True} for index in range(3)])
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    with store.registry() as reg:
        if change == 'claim':
            reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-2', 'owner', time.time() + 600))
        elif change == 'deferral':
            reg.conn.execute('INSERT INTO review_deferrals VALUES (?,?,?)', ('sample-2', 'Nick', time.time()))
        elif change == 'human':
            reg.add_annotation('sample-2', 'human', 'Nick', 'human', reg.annotation('sample-2', 'geometry')['path'])
        elif change == 'review':
            reg.add_review('sample-2', 'Nick', 'rejected')
        elif change == 'blocked':
            reg.conn.execute("UPDATE source_validation_groups SET state='blocked' WHERE group_key='old-sample-2'")
        elif change.startswith('new_'):
            kind = 'geometry' if change == 'new_geometry' else 'source_label'
            old = reg.annotation('sample-2', kind)
            reg.add_annotation('sample-2', kind, 'geometry' if kind == 'geometry' else 'fixture',
                               'changed-binding', old['path'], params=json.loads(old['params'] or '{}'))
        elif change in ('source_bytes', 'geometry_bytes'):
            kind = 'geometry' if change == 'geometry_bytes' else 'source_label'
            Path(reg.annotation('sample-2', kind)['path']).write_bytes(b'changed after plan')
        elif change == 'palette_bytes':
            params = json.loads(reg.annotation('sample-2', 'source_label')['params'])
            Path(params['palette_path']).write_bytes(b'changed pinned palette')
        else:
            settings.cfg['fixture_configuration_changed'] = True
        reg.conn.commit()
    before = snapshot(store)
    with pytest.raises(ValueError):
        audit.apply(settings, directory)
    assert snapshot(store) == before
    assert new_groups(store) == []


def test_plan_member_or_sample_tampering_cannot_change_the_authorized_group(tmp_path):
    settings, store = make_held(tmp_path)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    path = directory / 'plan.json'
    plan = json.loads(path.read_text())
    plan['groups'][0]['sample_ids'] = plan['groups'][0]['sample_ids'][:1]
    path.write_text(json.dumps(plan))
    before = snapshot(store)
    with pytest.raises(ValueError):
        audit.apply(settings, directory)
    assert snapshot(store) == before


def test_applied_batch_cannot_replay_even_when_external_receipt_is_missing(tmp_path):
    settings, store = make_held(tmp_path)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    (directory / 'applied.json').unlink()
    before = snapshot(store)
    with pytest.raises(ValueError):
        audit.apply(settings, directory)
    assert snapshot(store) == before


def test_changed_code_seal_rejects_plan_without_mutating_registry(tmp_path, monkeypatch):
    settings, store = make_held(tmp_path)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    original = audit.dependencies()
    monkeypatch.setattr(audit, 'dependencies', lambda: {**original, 'fixture-code-changed': 'new-sha'})
    before = snapshot(store)
    with pytest.raises(ValueError):
        audit.apply(settings, directory)
    assert snapshot(store) == before


def test_history_and_batch_receipts_are_immutable(tmp_path):
    settings, store = make_held(tmp_path)
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM held_conversion_audit_history') == 3
        assert reg.count('SELECT count(*) FROM held_conversion_audit_batches') == 1
        for statement in ("UPDATE held_conversion_audit_history SET previous_state='{}'",
                          'DELETE FROM held_conversion_audit_history',
                          "UPDATE held_conversion_audit_batches SET receipt='{}'",
                          'DELETE FROM held_conversion_audit_batches'):
            with pytest.raises(sqlite3.IntegrityError, match='Immutable'):
                reg.conn.execute(statement)
            reg.conn.rollback()


def test_previously_failed_group_cannot_be_restaged_as_fresh_audits(tmp_path):
    settings, store = make_held(tmp_path, [{'image_id': f'photo-{index}'} for index in range(5)])
    directory = tmp_path / 'audit-plan'
    audit.prepare(settings, directory)
    audit.apply(settings, directory)
    image_id = json.loads(new_groups(store)[0]['sample_ids'])[0]
    store.decide(image_id, 'corrected', 'Nick', store.detail(image_id)['revision'], str(uuid.uuid4()), lanes=[])
    refresh_groups(settings)
    with store.registry() as reg:
        reg.conn.execute("UPDATE source_conversion_checks SET state='flagged' WHERE state='awaiting_audit'")
        reg.conn.commit()
    before = snapshot(store)
    with pytest.raises(ValueError):
        audit.prepare(settings, tmp_path / 'fresh-plan')
    assert snapshot(store) == before
    assert not (tmp_path / 'fresh-plan').exists()
