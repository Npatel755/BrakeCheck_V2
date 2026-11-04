"""Individual draft examples never publish a conversion cohort or human vote."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time

import numpy as np
from PIL import Image
import pytest

from bcdata import trace_repair_diagnostics as diagnostic
from bcdata.bootstrap import atomic_json
from bcdata.recheck_flagged_conversions import digest, snapshot
from bcdata.review_store import REPAIR_SAMPLE_CLAUSE, ReviewStore
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    generation = tmp_path / 'reports' / 'sealed-evaluation'
    (generation / 'geometry').mkdir(parents=True)
    for name in diagnostic.SEALED_FILES:
        (generation / name).write_text('{}', encoding='utf-8')
    rows = []
    native = {'version': 'fixture-native', 'eligible_for_fresh_audit': True}
    tip = {'version': diagnostic.TIP_VERSION, 'eligible': True, 'verified_slots': [2], 'evidence': []}
    with store.registry() as reg:
        reg.conn.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                         ('original-group', 'original-batch', 'original-stratum', '[]', 'awaiting_audit', '{"keep":true}', time.time()))
        for index in range(3):
            image_id = f'sample-{index}'
            reg.update_image(image_id, source='a2d2', split='train')
            source_path = tmp_path / (image_id + '-source.png')
            Image.fromarray(np.zeros((874, 1164), np.uint8)).save(source_path)
            reg.add_annotation(image_id, 'source_label', 'fixture', 'native-human-v1', str(source_path),
                               params={'human': True, 'format': 'class_map'})
            geometry = dict(reg.annotation(image_id, 'geometry', 'geometry'))
            source = dict(reg.annotation(image_id, 'source_label'))
            reg.conn.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (image_id, 'original-batch', 'original-group', 'flagged', geometry['id'],
                              geometry['model_version'], geometry['path'], file_sha256(geometry['path']),
                              source['id'], source['path'], file_sha256(source['path']),
                              json.dumps({'reasons': ['low_mask_coverage', 'transverse_source_promoted_to_boundary'],
                                          'old_policy': 'must-remain'}), time.time()))
            reg.conn.commit()
            check = reg.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
            previous = snapshot(reg.conn, check, settings)
            candidate = json.loads(Path(geometry['path']).read_text())
            candidate['version'] = 'fixture-diagnostic-v1'
            path = generation / 'geometry' / (image_id + '.json')
            atomic_json(path, candidate)
            rows.append({'image_id': image_id, 'input': previous, 'eligible': True,
                         'candidate_path': str(path), 'candidate_sha256': file_sha256(path),
                         'candidate_version': candidate['version'], 'evidence': native, 'tip_evidence': tip,
                         'fingerprints': {geometry['path']: file_sha256(geometry['path']),
                                          source['path']: file_sha256(source['path'])},
                         'remaining_reasons': ['missing_model_comparison'],
                         'quality': {'score': .9, 'tier': 'high', 'issues': []}})
    report = {'code_sha256': {'fixture': 'unchanged'}}
    monkeypatch.setattr(diagnostic, 'dependencies', lambda: {'fixture': 'unchanged'})
    monkeypatch.setattr(diagnostic, 'verify', lambda *_: (report, rows, [], set(), {'a2d2': {'not_published': True}}))
    monkeypatch.setattr(diagnostic, 'retains_physical_traces', lambda *_: True)
    monkeypatch.setattr(diagnostic, 'measure_native', lambda *_: deepcopy(native))
    monkeypatch.setattr(diagnostic, 'measure_tip', lambda *_: deepcopy(tip))
    return settings, store, generation, tmp_path / 'reports' / 'diagnostic-plan', rows


def test_planning_is_read_only_and_balances_sessions(prepared):
    settings, store, generation, output, rows = prepared
    before = {str(path): file_sha256(path) for path in generation.rglob('*') if path.is_file()}
    plan = diagnostic.prepare(settings, generation, output, limit=2)
    assert len(plan['selected']) == 2 and plan['sessions'] == 2
    assert plan['eligible_train_candidates'] == 3
    assert plan['human_decisions_created'] == plan['cohorts_created'] == plan['cohorts_released'] == 0
    assert before == {str(path): file_sha256(path) for path in generation.rglob('*') if path.is_file()}
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM conversion_diagnostic_samples') == 0
        assert reg.count("SELECT count(*) FROM annotations WHERE model_version='fixture-diagnostic-v1'") == 0


def test_install_stays_flagged_and_preserves_original_groups_warnings_qc_and_votes(prepared):
    settings, store, generation, output, rows = prepared
    diagnostic.prepare(settings, generation, output)
    receipt = diagnostic.install(settings, output)
    assert receipt['installed'] == 3 and receipt['state'] == 'flagged' and receipt['diagnostic_only']
    assert receipt['human_decisions_created'] == receipt['cohorts_created'] == receipt['cohorts_released'] == 0
    backup = Path(receipt['backup'])
    assert (backup / 'metadata.sqlite').is_file() and (backup / 'human_labels.zip').is_file()
    assert receipt['seal']['module_sha256'] == file_sha256(diagnostic.MODULE)
    assert receipt['seal']['generation_sha256']['report.json'] == file_sha256(generation / 'report.json')
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM source_validation_groups') == 1
        assert dict(reg.conn.execute('SELECT * FROM source_validation_groups').fetchone()) == rows[0]['input']['group']
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 3
        assert reg.count('SELECT count(*) FROM images i WHERE ' + REPAIR_SAMPLE_CLAUSE) == 3
        assert reg.count('SELECT count(*) FROM reviews') == reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count('SELECT count(*) FROM annotations WHERE kind=\'human\'') == 0
        assert reg.count('SELECT count(*) FROM review_versions WHERE revision=1') == 3
        assert reg.conn.execute("SELECT value FROM local_meta WHERE key='revision'").fetchone()[0] == '1'
        for row in rows:
            current = reg.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (row['image_id'],)).fetchone()
            assert current['batch_id'] == 'original-batch' and current['group_key'] == 'original-group'
            details = json.loads(current['diagnostics'])
            assert details['reasons'] == json.loads(row['input']['check']['diagnostics'])['reasons']
            assert details['old_policy'] == 'must-remain' and details['diagnostic_only']
            assert dict(reg.conn.execute('SELECT * FROM qc WHERE image_id=?', (row['image_id'],)).fetchone()) == row['input']['qc']
            history = reg.conn.execute('SELECT * FROM trace_repair_diagnostic_history WHERE image_id=?', (row['image_id'],)).fetchone()
            assert json.loads(history['previous_state']) == row['input']
            assert json.loads(history['replacement'])['state'] == 'flagged'
        with pytest.raises(sqlite3.IntegrityError, match='Immutable'):
            reg.conn.execute("UPDATE trace_repair_diagnostic_history SET previous_state='{}'")
        reg.conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match='Immutable'):
            reg.conn.execute('DELETE FROM trace_repair_diagnostic_batches')


@pytest.mark.parametrize('protection', ['claim', 'deferral', 'review', 'human', 'moved_geometry', 'moved_source', 'blocked'])
def test_changed_or_protected_photo_is_skipped_without_touching_its_state(prepared, protection):
    settings, store, generation, output, rows = prepared
    diagnostic.prepare(settings, generation, output)
    with store.registry() as reg:
        if protection == 'claim':
            reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-0', 'phone-owner', time.time() + 600))
        elif protection == 'deferral':
            reg.conn.execute('INSERT INTO review_deferrals VALUES (?,?,?)', ('sample-0', 'Nick', time.time()))
        elif protection == 'review':
            reg.update_image('sample-0', review_status='approved', locked=1)
        elif protection == 'human':
            reg.add_annotation('sample-0', 'human', 'Nick', 'human', rows[0]['input']['geometry']['path'])
        elif protection.startswith('moved_'):
            kind = 'geometry' if protection == 'moved_geometry' else 'source_label'
            old = rows[0]['input']['geometry' if kind == 'geometry' else 'source']
            reg.add_annotation('sample-0', kind, 'geometry' if kind == 'geometry' else 'fixture', 'new-binding', old['path'])
        else:
            reg.conn.execute("UPDATE source_validation_groups SET state='blocked'")
        reg.conn.commit()
        before = snapshot(reg.conn, reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone(), settings)
    receipt = diagnostic.install(settings, output)
    assert receipt['installed'] == (0 if protection == 'blocked' else 2)
    with store.registry() as reg:
        after = snapshot(reg.conn, reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone(), settings)
        assert after == before


@pytest.mark.parametrize('damage', ['source', 'candidate', 'generation', 'module', 'retention', 'tip', 'native'])
def test_changed_bytes_or_failed_proof_cannot_partially_install(prepared, monkeypatch, damage, tmp_path):
    settings, store, generation, output, rows = prepared
    diagnostic.prepare(settings, generation, output)
    if damage == 'source':
        Path(rows[-1]['input']['source']['path']).write_bytes(b'changed source')
    elif damage == 'candidate':
        Path(rows[-1]['candidate_path']).write_bytes(b'changed draft')
    elif damage == 'generation':
        (generation / 'report.json').write_text('{"changed":true}')
    elif damage == 'module':
        dummy = tmp_path / 'different-module.py'
        dummy.write_text('changed diagnostic module')
        monkeypatch.setattr(diagnostic, 'MODULE', dummy)
    elif damage == 'retention':
        monkeypatch.setattr(diagnostic, 'retains_physical_traces', lambda *_: False)
    elif damage == 'tip':
        monkeypatch.setattr(diagnostic, 'measure_tip', lambda *_: None)
    else:
        monkeypatch.setattr(diagnostic, 'measure_native', lambda *_: {'eligible_for_fresh_audit': False})
    with pytest.raises(ValueError):
        diagnostic.install(settings, output)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM annotations WHERE model_version='fixture-diagnostic-v1'") == 0
        assert reg.count('SELECT count(*) FROM conversion_diagnostic_samples') == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 3
        assert reg.count('SELECT count(*) FROM review_versions') == 0


def test_commit_checks_detect_mid_transaction_mutation_and_roll_back(prepared, monkeypatch):
    settings, store, generation, output, rows = prepared
    diagnostic.prepare(settings, generation, output)
    original = diagnostic._fingerprints
    calls = 0
    def mutate_at_commit(row):
        nonlocal calls
        calls += 1
        if calls == 4:
            Path(row['candidate_path']).write_bytes(b'changed at commit')
        return original(row)
    monkeypatch.setattr(diagnostic, '_fingerprints', mutate_at_commit)
    with pytest.raises(ValueError, match='candidate bytes changed'):
        diagnostic.install(settings, output)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM annotations WHERE model_version='fixture-diagnostic-v1'") == 0
        assert reg.count('SELECT count(*) FROM conversion_diagnostic_samples') == 0
        assert reg.count('SELECT count(*) FROM review_versions') == 0


def test_existing_diagnostic_binding_is_preserved_and_test_split_never_selected(prepared):
    settings, store, generation, output, rows = prepared
    rows[1]['input']['image']['split'] = 'test'
    with store.registry() as reg:
        reg.update_image('sample-1', split='test')
        check = rows[0]['input']['check']
        reg.conn.execute('INSERT INTO conversion_diagnostic_samples VALUES (?,?,?,?,?,?)',
                         ('prior-diagnostic', 'sample-0', 'must-not-replace', check['geometry_id'], check['source_id'], time.time()))
        reg.conn.commit()
    plan = diagnostic.prepare(settings, generation, output)
    assert [row['image_id'] for row in plan['selected']] == ['sample-2']


def test_existing_audit_membership_cannot_become_a_diagnostic_vote_for_any_cohort(prepared):
    settings, store, generation, output, _ = prepared
    with store.registry() as reg:
        # Even a different historical group must not receive a new sample vote.
        reg.conn.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                         ('other-existing-cohort', 'other-batch', 'other-stratum', '["sample-0"]',
                          'awaiting_audit', '{}', time.time()))
        reg.conn.commit()
    plan = diagnostic.prepare(settings, generation, output)
    assert {row['image_id'] for row in plan['selected']} == {'sample-1', 'sample-2'}
    receipt = diagnostic.install(settings, output)
    assert set(receipt['sample_ids']) == {'sample-1', 'sample-2'}


def test_new_audit_membership_after_planning_is_protected_at_install(prepared):
    settings, store, generation, output, _ = prepared
    diagnostic.prepare(settings, generation, output)
    with store.registry() as reg:
        reg.conn.execute('INSERT INTO source_validation_groups VALUES (?,?,?,?,?,?,?)',
                         ('new-existing-cohort', 'other-batch', 'other-stratum', '["sample-0"]',
                          'awaiting_audit', '{}', time.time()))
        reg.conn.commit()
    receipt = diagnostic.install(settings, output)
    assert set(receipt['sample_ids']) == {'sample-1', 'sample-2'}


def test_session_round_robin_and_twenty_photo_cap():
    rows = [{'image_id': f'{session}-{index}', 'input': {'image': {'session_id': session}}}
            for session, count in [('busy', 30), ('small-a', 2), ('small-b', 1)] for index in range(count)]
    selected = diagnostic._balanced(rows, 20)
    assert len(selected) == 20
    assert len({row['input']['image']['session_id'] for row in selected[:3]}) == 3


def test_replay_is_rejected_even_if_external_receipt_was_lost(prepared):
    settings, _, generation, output, _ = prepared
    diagnostic.prepare(settings, generation, output)
    diagnostic.install(settings, output)
    (output / 'receipt.json').unlink()
    with pytest.raises(ValueError, match='already installed'):
        diagnostic.install(settings, output)


def test_sealed_generation_cannot_be_used_as_output(prepared):
    settings, _, generation, _, _ = prepared
    with pytest.raises(ValueError, match='outside'):
        diagnostic.prepare(settings, generation, generation / 'diagnostics')


@pytest.mark.parametrize('limit', [0, 21, True, 2.5])
def test_limit_cannot_expand_into_bulk_publication(prepared, limit):
    settings, _, generation, output, _ = prepared
    with pytest.raises(ValueError, match='between 1 and 20'):
        diagnostic.prepare(settings, generation, output, limit)


def test_real_sealed_verifier_is_required_even_when_bulk_route_is_unsupported(prepared, monkeypatch):
    from bcdata.apply_trace_repairs import verify
    from bcdata.repair_trace_backlog import POLICY, dependencies
    settings, _, generation, output, rows = prepared
    # These are disposable fixture records, never actual human decisions.
    controls = [{'image_id': f'fixture-control-{i}', 'eligible': False,
                 'input': {'image': {'image_id': f'fixture-control-{i}', 'source': 'a2d2',
                                     'review_status': 'approved'}}} for i in range(100)]
    atomic_json(generation / 'selection.json', {'backlog': [row['input'] for row in rows],
                                              'controls': [row['input'] for row in controls]})
    (generation / 'backlog.jsonl').write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    (generation / 'controls.jsonl').write_text('\n'.join(json.dumps(row) for row in controls), encoding='utf-8')
    atomic_json(generation / 'report.json', {
        'policy': POLICY, 'code_sha256': dependencies(), 'config_sha256': digest(settings.cfg), 'errors': 0,
        'selection_sha256': file_sha256(generation / 'selection.json'),
        'backlog_sha256': file_sha256(generation / 'backlog.jsonl'),
        'controls_sha256': file_sha256(generation / 'controls.jsonl')})
    monkeypatch.setattr(diagnostic, 'verify', verify)
    monkeypatch.setattr(diagnostic, 'dependencies', dependencies)
    plan = diagnostic.prepare(settings, generation, output)
    assert len(plan['selected']) == 3 and plan['human_control_evidence'] == {}
    (generation / 'controls.jsonl').write_text('unsealed modification', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        diagnostic.install(settings, output)


def test_revalidation_recomputes_real_native_tip_receipt_from_exact_source_pixels(tmp_path):
    from tests.test_source_tip_evidence import fixture
    from bcdata.source_tip_evidence import measure as measure_tip
    geometry, paint, native = fixture()
    geometry.update(image_id='real-proof-fixture', version='fixture-tip-v1')
    generation = tmp_path / 'generation'
    (generation / 'geometry').mkdir(parents=True)
    candidate_path = generation / 'geometry' / 'candidate.json'
    original_path = tmp_path / 'original.json'
    source_path = tmp_path / 'native.png'
    atomic_json(candidate_path, geometry)
    atomic_json(original_path, geometry)
    Image.fromarray((paint * 2).astype(np.uint8)).save(source_path)
    fingerprints = {str(original_path): file_sha256(original_path), str(source_path): file_sha256(source_path)}
    row = {'image_id': geometry['image_id'], 'candidate_path': str(candidate_path),
           'candidate_sha256': file_sha256(candidate_path), 'candidate_version': geometry['version'],
           'fingerprints': fingerprints, 'tip_evidence': measure_tip(geometry, paint, native), 'evidence': native,
           'input': {'image': {'width': geometry['width'], 'height': geometry['height']},
                     'geometry': {'id': 1, 'path': str(original_path)},
                     'source': {'id': 2, 'path': str(source_path), 'params': '{"human":true,"format":"class_map"}'},
                     'check': {'geometry_id': 1, 'geometry_path': str(original_path),
                               'geometry_sha256': fingerprints[str(original_path)], 'source_id': 2,
                               'source_path': str(source_path), 'source_sha256': fingerprints[str(source_path)]}}}
    proof = diagnostic._revalidate(row, generation)
    assert proof['tip_evidence'] == row['tip_evidence']
    assert proof['native_target_evidence']['eligible_for_fresh_audit']
    row['tip_evidence']['verified_slots'] = [3]
    with pytest.raises(ValueError, match='receipt differs'):
        diagnostic._revalidate(row, generation)
