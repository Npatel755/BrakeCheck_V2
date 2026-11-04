import json
from pathlib import Path

from bcdata.conversion_diagnostics import prepare, family
from bcdata.source_validation import refresh_groups
from tests.test_apply_trace_repairs import prepared_fixture
from tests.test_review import decide


def test_diagnostic_examples_never_release_the_related_backlog(tmp_path, monkeypatch):
    settings, store, _, records = prepared_fixture(tmp_path, monkeypatch)
    directory = tmp_path / 'reports' / 'diagnostic-selection'
    report = prepare(settings, directory, apply=True)
    assert report['installed'] == 3 and report['family_count'] == 1
    assert store.stats()['conversion_diagnostic_samples'] == 3
    image = store.next_image('diagnostic-owner', status='repair_sample')
    assert image['image_id'] in {row['image_id'] for row in records}
    decide(store, image_id=image['image_id'], revision=0)
    refresh_groups(settings)
    assert store.stats()['conversion_diagnostic_samples'] == 2
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM source_validation_groups') == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='flagged'") == 3
        assert reg.count('SELECT count(*) FROM reviews') == 1


def test_stale_geometry_cannot_remain_in_the_repair_example_queue(tmp_path, monkeypatch):
    settings, store, _, records = prepared_fixture(tmp_path, monkeypatch)
    prepare(settings, tmp_path / 'reports' / 'diagnostics', apply=True)
    with store.registry() as reg:
        for row in records:
            annotation = row['input']['geometry']
            reg.add_annotation(row['image_id'], 'geometry', 'geometry', 'superseded-fixture', annotation['path'])
    assert store.stats()['conversion_diagnostic_samples'] == 0
    assert store.next_image('stale-check', status='repair_sample') is None


def test_repair_examples_support_batch_and_skip_without_releasing_cohorts(tmp_path, monkeypatch):
    settings, store, _, records = prepared_fixture(tmp_path, monkeypatch)
    prepare(settings, tmp_path / 'reports' / 'diagnostics', apply=True)
    batch = store.batch('diagnostic-owner', status='repair_sample')
    assert len(batch) == 3
    image = batch[0]
    # Real reviewer deferral remains a separate choice, not an approval.
    store.defer(image['image_id'], image['revision'], 'Nick', 'diagnostic-owner')
    assert store.stats()['conversion_diagnostic_samples'] == 2


def test_dominant_identity_issue_is_not_hidden_by_a_coverage_warning():
    assert family(['source_boundary_uncovered', 'source_slot_disjoint']) == 'lane-identity'
    assert family(['low_mask_coverage']) == 'source-coverage'
