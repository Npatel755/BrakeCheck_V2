"""Shared-row ordering examples remain sealed, individual human diagnostics."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from bcdata import trace_repair_diagnostics as diagnostic
from bcdata.bootstrap import atomic_json
from bcdata.recheck_flagged_conversions import snapshot
from bcdata.review_store import REPAIR_SAMPLE_CLAUSE
from bcdata.teachers.base import file_sha256
from tests.test_trace_repair_diagnostics import prepared


@pytest.fixture
def order_prepared(prepared, monkeypatch):
    receipt = {'eligible': True, 'version': diagnostic.ORDER_VERSION,
               'verified_slots': [2, 3], 'geometry_changed': False, 'source_only': True}
    for row in prepared[-1]:
        row['tip_evidence'] = None
        row['shared_order_evidence'] = deepcopy(receipt)
    monkeypatch.setattr(diagnostic, 'measure_shared_order', lambda *_: deepcopy(receipt))
    return prepared


def plan_order(prepared, **kwargs):
    settings, _, generation, output, _ = prepared
    return diagnostic.prepare(settings, generation, output, mechanism='shared-order', **kwargs)


def assert_no_install(prepared):
    _, store, _, _, _ = prepared
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM annotations WHERE model_version='fixture-diagnostic-v1'") == 0
        assert reg.count('SELECT count(*) FROM conversion_diagnostic_samples') == 0
        assert reg.count('SELECT count(*) FROM review_versions') == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 3


def test_order_plan_is_read_only_sealed_and_balanced(order_prepared):
    _, store, generation, _, _ = order_prepared
    before = {str(path): file_sha256(path) for path in generation.rglob('*') if path.is_file()}
    plan = plan_order(order_prepared, limit=2)
    assert plan['mechanism'] == 'shared-order'
    assert len(plan['selected']) == plan['sessions'] == 2
    assert plan['eligible_train_candidates'] == 3
    assert plan['training_held']
    assert plan['human_decisions_created'] == plan['cohorts_created'] == plan['cohorts_released'] == 0
    assert before == {str(path): file_sha256(path) for path in generation.rglob('*') if path.is_file()}
    for row in plan['selected']:
        assert row['proof']['shared_order_evidence']['version'] == diagnostic.ORDER_VERSION
        assert row['proof']['tip_evidence'] is None
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM conversion_diagnostic_samples') == 0


def test_order_install_keeps_original_holds_groups_qc_and_human_decisions(order_prepared):
    settings, store, _, output, rows = order_prepared
    plan_order(order_prepared)
    receipt = diagnostic.install(settings, output)
    assert receipt['mechanism'] == 'shared-order' and receipt['installed'] == 3
    assert receipt['state'] == 'flagged' and receipt['diagnostic_only'] and receipt['training_held']
    assert receipt['human_decisions_created'] == receipt['cohorts_created'] == receipt['cohorts_released'] == 0
    backup = Path(receipt['backup'])
    assert (backup / 'metadata.sqlite').is_file() and (backup / 'human_labels.zip').is_file()
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM source_validation_groups') == 1
        assert reg.count('SELECT count(*) FROM images i WHERE ' + REPAIR_SAMPLE_CLAUSE) == 3
        assert reg.count('SELECT count(*) FROM reviews') == reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count("SELECT count(*) FROM annotations WHERE kind='human'") == 0
        families = reg.conn.execute('SELECT DISTINCT family FROM conversion_diagnostic_samples').fetchall()
        assert [row[0] for row in families] == ['a2d2:native-shared-order']
        for row in rows:
            current = reg.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (row['image_id'],)).fetchone()
            assert current['state'] == 'flagged'
            assert current['batch_id'] == 'original-batch' and current['group_key'] == 'original-group'
            details = json.loads(current['diagnostics'])
            assert details['reasons'] == json.loads(row['input']['check']['diagnostics'])['reasons']
            assert details['old_policy'] == 'must-remain'
            assert details['repair_diagnostic']['shared_order_evidence'] == row['shared_order_evidence']
            assert dict(reg.conn.execute('SELECT * FROM qc WHERE image_id=?', (row['image_id'],)).fetchone()) == row['input']['qc']


def test_default_diagnostics_still_select_tip_receipts_only(prepared, monkeypatch):
    def unexpected(*_):
        pytest.fail('Tip-only diagnostics must not invent shared-row ordering evidence')
    monkeypatch.setattr(diagnostic, 'measure_shared_order', unexpected)
    settings, _, generation, output, _ = prepared
    plan = diagnostic.prepare(settings, generation, output)
    assert plan['mechanism'] == 'tip' and len(plan['selected']) == 3
    assert all('shared_order_evidence' not in row['proof'] for row in plan['selected'])
    assert diagnostic.install(settings, output)['mechanism'] == 'tip'


@pytest.mark.parametrize('problem', ['wrong_version', 'ineligible', 'missing', 'test', 'wrong_source', 'not_curated'])
def test_order_selection_requires_its_own_valid_train_source_receipt(order_prepared, problem):
    _, _, _, _, rows = order_prepared
    if problem == 'wrong_version':
        rows[0]['shared_order_evidence']['version'] += '-changed'
    elif problem == 'ineligible':
        rows[0]['shared_order_evidence']['eligible'] = False
    elif problem == 'missing':
        rows[0]['shared_order_evidence'] = None
    else:
        key, value = {'test': ('split', 'test'), 'wrong_source': ('source', 'comma10k'),
                      'not_curated': ('status', 'held')}[problem]
        rows[0]['input']['image'][key] = value
    plan = plan_order(order_prepared)
    assert {row['image_id'] for row in plan['selected']} == {'sample-1', 'sample-2'}


@pytest.mark.parametrize('damage', ['source', 'candidate', 'generation', 'module', 'retention', 'order', 'native'])
def test_order_source_bytes_and_fresh_proofs_are_required_before_any_install(order_prepared, monkeypatch, damage, tmp_path):
    settings, _, generation, output, rows = order_prepared
    plan_order(order_prepared)
    if damage == 'source':
        Path(rows[-1]['input']['source']['path']).write_bytes(b'changed source')
    elif damage == 'candidate':
        Path(rows[-1]['candidate_path']).write_bytes(b'changed candidate')
    elif damage == 'generation':
        (generation / 'report.json').write_text('{"changed":true}')
    elif damage == 'module':
        module = tmp_path / 'different-module.py'
        module.write_text('changed diagnostic code')
        monkeypatch.setattr(diagnostic, 'MODULE', module)
    elif damage == 'retention':
        monkeypatch.setattr(diagnostic, 'retains_physical_traces', lambda *_: False)
    elif damage == 'order':
        monkeypatch.setattr(diagnostic, 'measure_shared_order', lambda *_: None)
    else:
        monkeypatch.setattr(diagnostic, 'measure_native', lambda *_: {'eligible_for_fresh_audit': False})
    with pytest.raises(ValueError):
        diagnostic.install(settings, output)
    assert_no_install(order_prepared)


def test_order_diagnostics_revalidate_existing_tip_proof_too(order_prepared, monkeypatch):
    settings, _, _, output, rows = order_prepared
    tip = {'version': diagnostic.TIP_VERSION, 'eligible': True, 'verified_slots': [2], 'evidence': []}
    for row in rows:
        row['tip_evidence'] = deepcopy(tip)
    plan = plan_order(order_prepared)
    assert all(row['proof']['tip_evidence'] == tip for row in plan['selected'])
    monkeypatch.setattr(diagnostic, 'measure_tip', lambda *_: None)
    with pytest.raises(ValueError, match='receipt differs'):
        diagnostic.install(settings, output)
    assert_no_install(order_prepared)


@pytest.mark.parametrize('protection', ['claim', 'deferral', 'human', 'blocked'])
def test_order_diagnostics_respect_new_review_claims_and_existing_review_work(order_prepared, protection):
    settings, store, _, output, rows = order_prepared
    plan_order(order_prepared)
    with store.registry() as reg:
        if protection == 'claim':
            reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-0', 'reviewer', time.time() + 600))
        elif protection == 'deferral':
            reg.conn.execute('INSERT INTO review_deferrals VALUES (?,?,?)', ('sample-0', 'Nick', time.time()))
        elif protection == 'human':
            reg.add_annotation('sample-0', 'human', 'Nick', 'human', rows[0]['input']['geometry']['path'])
        else:
            reg.conn.execute("UPDATE source_validation_groups SET state='blocked'")
        reg.conn.commit()
        before = snapshot(reg.conn, reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone(), settings)
    receipt = diagnostic.install(settings, output)
    assert receipt['installed'] == (0 if protection == 'blocked' else 2)
    with store.registry() as reg:
        after = snapshot(reg.conn, reg.conn.execute("SELECT * FROM source_conversion_checks WHERE image_id='sample-0'").fetchone(), settings)
        assert before == after


def test_order_diagnostic_replay_is_rejected_from_database_receipt(order_prepared):
    settings, _, _, output, _ = order_prepared
    plan_order(order_prepared)
    diagnostic.install(settings, output)
    (output / 'receipt.json').unlink()
    with pytest.raises(ValueError, match='already installed'):
        diagnostic.install(settings, output)


def test_order_mechanism_is_bound_to_the_plan_identity(order_prepared):
    settings, _, _, output, _ = order_prepared
    plan = plan_order(order_prepared)
    plan['mechanism'] = 'tip'
    atomic_json(output / 'plan.json', plan)
    with pytest.raises(ValueError):
        diagnostic.install(settings, output)
    assert_no_install(order_prepared)


@pytest.mark.parametrize('mechanism', ['anything', '', None, True])
def test_unknown_diagnostic_mechanism_is_rejected_before_output_creation(prepared, mechanism):
    settings, _, generation, output, _ = prepared
    with pytest.raises(ValueError, match='Unknown diagnostic mechanism'):
        diagnostic.prepare(settings, generation, output, mechanism=mechanism)
    assert not output.exists()


def test_revalidation_recomputes_real_shared_order_proof_from_exact_native_pixels(tmp_path):
    import numpy as np
    from PIL import Image
    from bcdata.source_shared_order_evidence import measure
    from tests.test_source_shared_order_evidence import fixture

    geometry, paint, native = fixture()
    geometry.update(image_id='real-order-proof-fixture', version='fixture-shared-order-v1')
    generation = tmp_path / 'generation'
    (generation / 'geometry').mkdir(parents=True)
    candidate = generation / 'geometry' / 'candidate.json'
    original = tmp_path / 'original.json'
    source = tmp_path / 'native.png'
    atomic_json(candidate, geometry)
    atomic_json(original, geometry)
    Image.fromarray((paint * 2).astype(np.uint8)).save(source)
    fingerprints = {str(original): file_sha256(original), str(source): file_sha256(source)}
    receipt = measure(geometry, paint, native)
    row = {'image_id': geometry['image_id'], 'candidate_path': str(candidate),
           'candidate_sha256': file_sha256(candidate), 'candidate_version': geometry['version'],
           'fingerprints': fingerprints, 'shared_order_evidence': receipt, 'evidence': native,
           'input': {'image': {'width': geometry['width'], 'height': geometry['height']},
                     'geometry': {'id': 1, 'path': str(original)},
                     'source': {'id': 2, 'path': str(source), 'params': '{"human":true,"format":"class_map"}'},
                     'check': {'geometry_id': 1, 'geometry_path': str(original),
                               'geometry_sha256': fingerprints[str(original)], 'source_id': 2,
                               'source_path': str(source), 'source_sha256': fingerprints[str(source)]}}}
    proof = diagnostic._revalidate(row, generation, mechanism='shared-order')
    assert proof['shared_order_evidence'] == receipt
    assert proof['native_target_evidence']['eligible_for_fresh_audit']
    assert proof['tip_evidence'] is None
    assert proof['shared_order_evidence']['adjacent_pairs'][0]['shared_source_rows'] == 301
    row['shared_order_evidence']['verified_slots'] = [2, 3]
    with pytest.raises(ValueError, match='receipt differs'):
        diagnostic._revalidate(row, generation, mechanism='shared-order')
