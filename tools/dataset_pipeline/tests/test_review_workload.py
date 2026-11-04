"""Keep sample audits separate from unresolved automatic conversion work."""
from pathlib import Path

from bcdata.review_export import export_reviewed
from bcdata.source_validation import refresh_groups
from tests.test_source_validation import setup_source, approve_samples


def test_workload_distinguishes_audit_samples_and_flagged_exceptions(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    with store.registry() as reg:
        reg.conn.execute("UPDATE source_conversion_checks SET state='flagged' WHERE image_id=?", (held,))
        reg.conn.commit()
    stats = store.stats()
    assert stats['pending'] == 3
    assert stats['source_audit_samples'] == 2
    assert stats['flagged_source_conversions'] == 1
    assert stats['other_manual_pending'] == 0
    assert store.next_image('sample-reviewer', status='audit_sample')['image_id'] in samples
    assert store.next_image('exception-reviewer', status='flagged')['image_id'] == held
    assert stats['reviewed'] == stats['accepted'] == 0
    assert export_reviewed(settings)['exported'] == 0
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count("SELECT count(*) FROM annotations WHERE kind='human'") == 0


def test_changed_conversion_remains_an_explicit_individual_review_case(tmp_path):
    settings, store, samples, held = setup_source(tmp_path)
    approve_samples(store, samples)
    refresh_groups(settings)
    with store.registry() as reg:
        ann = reg.annotation(held, 'geometry', 'geometry')
        path = settings.root / 'changed-geometry.json'
        path.write_bytes(Path(ann['path']).read_bytes())
        reg.add_annotation(held, 'geometry', 'geometry', ann['model_version'], str(path))
    stats = store.stats()
    assert stats['pending'] == stats['other_manual_pending'] == 1
    assert stats['source_audit_samples'] == stats['flagged_source_conversions'] == 0
    assert stats['source_validated'] == 0
    assert export_reviewed(settings)['exported'] == len(samples)
