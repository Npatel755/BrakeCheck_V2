"""Repair publication preserves actual votes, source bytes and audit gates."""
from copy import deepcopy
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pytest

from bcdata import apply_trace_repairs as installer
from bcdata.bootstrap import atomic_json
from bcdata.recheck_flagged_conversions import snapshot
from bcdata.repair_trace_backlog import dependencies
from bcdata.review_store import ReviewStore
from bcdata.source_validation import refresh_groups
from bcdata.teachers.base import file_sha256
from tests.test_review import build_review_fixture, decide


def controls(count=20, source='a2d2'):
    return [{'image_id': f'control-{i}', 'eligible': True, 'regression': False,
             'input': {'image': {'source': source, 'review_status': 'approved'}},
             'human_comparison': {'candidate': {'tp': 2, 'fp': 0, 'fn': 0}}} for i in range(count)]


def test_only_supported_sources_can_receive_fresh_repair_audits():
    good = controls()
    bad = controls(source='comma10k')
    for row in bad[:2]:
        row['human_comparison']['candidate']['fp'] = 1
    supported, evidence = installer.supported_sources(good + bad)
    assert supported == {'a2d2'}
    assert evidence['comma10k']['source_supported_match_fraction'] == .9
    good[0]['regression'] = True
    assert installer.supported_sources(good)[0] == set()
    assert installer.supported_sources(controls(19))[0] == set()


def test_established_route_cannot_certify_an_unsupported_new_ordering_route():
    good = controls(40)
    new = controls(5)
    for row in new:
        row['refinement'] = {'version': 'native-foreground-order-v1'}
    supported, evidence = installer.supported_sources(good + new)
    assert supported == {'a2d2'}
    routes = evidence['a2d2']['route_evidence']
    assert routes['native-trace']['eligible_for_fresh_audits']
    assert not routes['native-foreground-order-v1']['eligible_for_fresh_audits']


def test_teacher_disagreement_does_not_change_the_source_only_reconstruction_method():
    rows = controls(40)
    for row in rows[:19]:
        row['audit_risks'] = ['model_source_count_disagreement']
    _, evidence = installer.supported_sources(rows)
    routes = evidence['a2d2']['route_evidence']
    assert routes['native-trace']['qualifying_controls'] == 40
    assert routes['native-trace']['eligible_for_fresh_audits']


def test_independent_order_controls_include_easy_cases_and_do_not_hide_regressions():
    rows = controls(20)
    for row in rows:
        row['alternative_controls'] = [{'eligible': True,
            'refinement': {'version': 'native-foreground-order-v1'},
            'human_comparison': {'candidate': {'tp': 2, 'fp': 0, 'fn': 0}}, 'regression': False}]
    _, evidence = installer.supported_sources(rows)
    assert evidence['a2d2']['distinct_human_controls'] == 20
    assert evidence['a2d2']['route_evidence']['native-foreground-order-v1']['eligible_for_fresh_audits']
    rows[0]['alternative_controls'][0]['regression'] = True
    _, evidence = installer.supported_sources(rows)
    assert not evidence['a2d2']['route_evidence']['native-foreground-order-v1']['eligible_for_fresh_audits']
    assert evidence['a2d2']['route_evidence']['native-trace']['eligible_for_fresh_audits']


def prepared_fixture(tmp_path, monkeypatch):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    directory = tmp_path / 'reports' / 'sealed-repair'
    (directory / 'geometry').mkdir(parents=True)
    atomic_json(directory / 'report.json', {'disposable_fixture': True})
    records = []
    with store.registry() as reg:
        for index in range(3):
            image_id = f'sample-{index}'
            reg.update_image(image_id, split='train')
            source_path = tmp_path / (image_id + '-source.png')
            Image.fromarray(np.zeros((874, 1164), np.uint8)).save(source_path)
            reg.add_annotation(image_id, 'source_label', 'fixture', 'human-source-v1', str(source_path),
                               params={'human': True, 'format': 'class_map'})
            geometry = dict(reg.annotation(image_id, 'geometry', 'geometry'))
            source = dict(reg.annotation(image_id, 'source_label'))
            reg.conn.execute('INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (image_id, 'original', 'original:flagged', 'flagged', geometry['id'],
                              geometry['model_version'], geometry['path'], file_sha256(geometry['path']),
                              source['id'], source['path'], file_sha256(source['path']),
                              json.dumps({'reasons': ['low_mask_coverage']}), time.time()))
            reg.conn.commit()
            check = reg.conn.execute('SELECT * FROM source_conversion_checks WHERE image_id=?', (image_id,)).fetchone()
            previous = snapshot(reg.conn, check, settings)
            candidate = json.loads(Path(geometry['path']).read_text())
            candidate['version'] = 'fixture-repaired-v1'
            path = directory / 'geometry' / (image_id + '.json')
            atomic_json(path, candidate)
            records.append({'image_id': image_id, 'input': previous, 'eligible': True,
                            'candidate_path': str(path), 'candidate_sha256': file_sha256(path),
                            'candidate_version': candidate['version'],
                            'fingerprints': {geometry['path']: file_sha256(geometry['path']),
                                             source['path']: file_sha256(source['path'])},
                            'evidence': {'eligible_for_fresh_audit': True}, 'refinement': {'fixture': True},
                            'quality': {'score': .9, 'tier': 'high', 'issues': []}})
    report = {'code_sha256': dependencies()}
    monkeypatch.setattr(installer, 'verify', lambda *args: (report, records, [], {'comma10k'}, {}))
    return settings, store, directory, records


def test_publication_creates_three_real_audit_samples_without_approvals(tmp_path, monkeypatch):
    settings, store, directory, records = prepared_fixture(tmp_path, monkeypatch)
    result = installer.apply(settings, directory)
    assert result['installed'] == 3 and result['counts'] == {'sample': 3}
    assert result['groups'] == 1 and result['human_decisions_created'] == 0
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM reviews') == 0
        assert reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
    decide(store, revision=1)
    refresh_groups(settings)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
    for index in (1, 2):
        decide(store, image_id=f'sample-{index}', revision=1)
    refresh_groups(settings)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_validation_groups WHERE state='passed'") == 1
        group = reg.conn.execute("SELECT evidence FROM source_validation_groups WHERE state='passed'").fetchone()
        assert json.loads(group['evidence'])['policy'] == installer.POLICY
    for record in records:
        for path, expected in record['fingerprints'].items():
            assert file_sha256(path) == expected


def test_changed_source_aborts_the_entire_repair_transaction(tmp_path, monkeypatch):
    settings, store, directory, records = prepared_fixture(tmp_path, monkeypatch)
    Path(records[-1]['input']['source']['path']).write_bytes(b'changed source')
    with pytest.raises(ValueError, match='bytes changed'):
        installer.apply(settings, directory)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 3
        assert reg.count('SELECT count(*) FROM source_validation_groups') == 0
        assert reg.count("SELECT count(*) FROM annotations WHERE model_version='fixture-repaired-v1'") == 0


def test_a_review_claim_is_preserved_and_excluded_from_bulk_repairs(tmp_path, monkeypatch):
    settings, store, directory, records = prepared_fixture(tmp_path, monkeypatch)
    with store.registry() as reg:
        reg.conn.execute('INSERT INTO review_claims VALUES (?,?,?)', ('sample-0', 'phone-reviewer', time.time() + 300))
        reg.conn.commit()
    result = installer.apply(settings, directory)
    assert result['installed'] == 2 and len(result['skipped']) == 1
    with store.registry() as reg:
        assert reg.conn.execute('SELECT owner FROM review_claims WHERE image_id=?', ('sample-0',)).fetchone()[0] == 'phone-reviewer'
        assert reg.conn.execute('SELECT state FROM source_conversion_checks WHERE image_id=?', ('sample-0',)).fetchone()[0] == 'flagged'


def test_teacher_disagreement_has_separate_actual_audit_samples(tmp_path, monkeypatch):
    settings, store, directory, records = prepared_fixture(tmp_path, monkeypatch)
    records[0]['audit_risks'] = ['model_source_count_disagreement']
    result = installer.apply(settings, directory)
    assert result['groups'] == 2 and result['counts'] == {'sample': 3}
    with store.registry() as reg:
        groups = reg.conn.execute('SELECT stratum,sample_ids FROM source_validation_groups').fetchall()
        risk = [row for row in groups if 'teacher-disagreement' in row['stratum']]
        assert len(risk) == 1 and json.loads(risk[0]['sample_ids']) == ['sample-0']
        assert reg.count('SELECT COUNT(*) FROM reviews') == 0
