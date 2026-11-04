"""Source-backed shared-row ordering has a separate, narrow audit route."""
from copy import deepcopy

import pytest

from bcdata import apply_trace_repairs as installer
from bcdata import repair_trace_backlog as evaluator
from tests.test_tip_recheck_integration import controls, examine_fixture


ORDER_VERSION = 'native-shared-paint-order-v1'
TIP_VERSION = 'native-distal-tip-evidence-v1'
COMBINED_VERSION = TIP_VERSION + '+' + ORDER_VERSION


def supported_order(monkeypatch):
    receipt = {'eligible': True, 'version': ORDER_VERSION, 'geometry_changed': False,
               'requires_fresh_human_audit': True, 'verified_slots': [2, 3]}
    calls = []

    def measure(draft, paint, native):
        calls.append((draft, paint, native))
        return deepcopy(receipt) if native.get('eligible_for_fresh_audit') is True else None

    monkeypatch.setattr(evaluator, 'measure_shared_order', measure)
    return receipt, calls


def test_shared_order_only_removes_the_supported_slot_conflict(tmp_path, monkeypatch):
    receipt, calls = supported_order(monkeypatch)
    result, output = examine_fixture(tmp_path, monkeypatch, reasons=['slot_conflict'])
    assert result.get('error') is None
    assert result['eligible'] and result['remaining_reasons'] == []
    assert result['shared_order_evidence'] == receipt
    assert result['tip_evidence'] is None and result['refinement'] is None
    assert len(calls) == 1 and len(list(output.glob('*.json'))) == 1


@pytest.mark.parametrize('overrides', [
    {'source': 'comma10k'},
    {'complete': False},
    {'retain': False},
    {'blocked': True},
    {'reasons': ['slot_conflict', 'fatal_source_identity_error']},
])
def test_shared_order_cannot_bypass_other_source_and_qc_gates(tmp_path, monkeypatch, overrides):
    supported_order(monkeypatch)
    result, output = examine_fixture(tmp_path, monkeypatch,
                                     **{'reasons': ['slot_conflict'], **overrides})
    assert result.get('error') is None
    assert not result['eligible'] and not list(output.glob('*.json'))
    if overrides.get('source') == 'comma10k' or overrides.get('complete') is False:
        assert result['shared_order_evidence'] is None
        assert 'slot_conflict' in result['remaining_reasons']
    if overrides.get('reasons'):
        assert result['remaining_reasons'] == ['fatal_source_identity_error']


def test_shared_order_measurement_only_runs_for_its_own_warning(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail('Shared-row ordering must only assess its own QC warning')
    monkeypatch.setattr(evaluator, 'measure_shared_order', unexpected)
    result, _ = examine_fixture(tmp_path, monkeypatch, reasons=[])
    assert result.get('error') is None
    assert result['eligible'] and result['shared_order_evidence'] is None


def test_failed_order_receipt_preserves_the_warning_and_does_not_write_a_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluator, 'measure_shared_order', lambda *args: None)
    result, output = examine_fixture(tmp_path, monkeypatch, reasons=['slot_conflict'])
    assert result.get('error') is None
    assert not result['eligible'] and result['remaining_reasons'] == ['slot_conflict']
    assert result['shared_order_evidence'] is None and not list(output.glob('*.json'))


def test_tip_and_shared_order_both_need_their_own_receipts(tmp_path, monkeypatch):
    receipt, _ = supported_order(monkeypatch)
    result, _ = examine_fixture(tmp_path, monkeypatch,
        reasons=['slot_conflict', 'transverse_source_promoted_to_boundary'])
    assert result.get('error') is None
    assert result['eligible'] and result['remaining_reasons'] == []
    assert result['shared_order_evidence'] == receipt
    assert result['tip_evidence']['version'] == TIP_VERSION
    assert installer.route_key(result) == COMBINED_VERSION


def order_controls(count=20, *, tip=False, approved=20):
    rows = controls(count, tip=tip, approved=approved)
    for row in rows:
        row['shared_order_evidence'] = {'eligible': True, 'version': ORDER_VERSION}
    return rows


def test_generic_and_twenty_tip_controls_cannot_certify_nineteen_order_controls():
    rows = controls(40) + controls(20, tip=True) + order_controls(19)
    _, evidence = installer.supported_sources(rows)
    routes = evidence['a2d2']['route_evidence']
    assert routes['native-trace']['eligible_for_fresh_audits']
    assert routes[TIP_VERSION]['eligible_for_fresh_audits']
    assert routes[ORDER_VERSION]['qualifying_controls'] == 19
    assert not routes[ORDER_VERSION]['eligible_for_fresh_audits']


@pytest.mark.parametrize('problem', ['too_few', 'too_few_approved', 'low_match', 'regression'])
def test_order_route_keeps_the_existing_human_evidence_thresholds(problem):
    rows = order_controls(19 if problem == 'too_few' else 20,
                          approved=9 if problem == 'too_few_approved' else 20)
    if problem == 'low_match':
        for row in rows[:2]:
            row['human_comparison']['candidate']['fp'] = 1
    if problem == 'regression':
        rows[0]['regression'] = True
    assert installer.supported_sources(rows)[0] == set()


def test_twenty_order_controls_only_certify_the_order_route():
    supported, evidence = installer.supported_sources(order_controls(20, approved=10))
    assert supported == {'a2d2'}
    routes = evidence['a2d2']['route_evidence']
    assert set(routes) == {ORDER_VERSION}
    assert routes[ORDER_VERSION]['eligible_for_fresh_audits']


def test_combined_tip_order_is_separate_from_each_individually_supported_route():
    rows = controls(20, tip=True) + order_controls(20) + order_controls(19, tip=True)
    _, evidence = installer.supported_sources(rows)
    routes = evidence['a2d2']['route_evidence']
    assert routes[TIP_VERSION]['eligible_for_fresh_audits']
    assert routes[ORDER_VERSION]['eligible_for_fresh_audits']
    assert routes[COMBINED_VERSION]['qualifying_controls'] == 19
    assert not routes[COMBINED_VERSION]['eligible_for_fresh_audits']
    _, evidence = installer.supported_sources(order_controls(20, tip=True, approved=10))
    assert set(evidence['a2d2']['route_evidence']) == {COMBINED_VERSION}
    assert evidence['a2d2']['route_evidence'][COMBINED_VERSION]['eligible_for_fresh_audits']


@pytest.mark.parametrize(('tip', 'order', 'expected'), [
    (None, {'eligible': True, 'version': ORDER_VERSION}, ORDER_VERSION),
    ({'eligible': True, 'version': TIP_VERSION}, None, TIP_VERSION),
    ({'eligible': True, 'version': TIP_VERSION}, {'eligible': True, 'version': ORDER_VERSION}, COMBINED_VERSION),
    ({'eligible': True, 'version': TIP_VERSION + '-changed'}, {'eligible': True, 'version': ORDER_VERSION}, ORDER_VERSION),
    ({'eligible': True, 'version': TIP_VERSION}, {'eligible': True, 'version': ORDER_VERSION + '-changed'}, TIP_VERSION),
    (None, {'eligible': False, 'version': ORDER_VERSION}, 'native-trace'),
    (None, {'eligible': True, 'version': ORDER_VERSION + '-changed'}, 'native-trace'),
])
def test_route_identity_requires_exact_valid_receipt_versions(tip, order, expected):
    assert installer.route_key({'tip_evidence': tip, 'shared_order_evidence': order}) == expected


def test_order_helper_is_sealed_with_the_evaluation_dependencies():
    assert any(path.endswith('source_shared_order_evidence.py') for path in evaluator.dependencies())
