import json

import pytest

from bcdata.db import Registry
from bcdata.repair_missing_splits import run
from bcdata.expand_dashcam import split_for
from tests.test_review import build_review_fixture, decide
from bcdata.review_store import ReviewStore
from bcdata.review_export import export_reviewed


def test_missing_split_inherits_recording_and_keeps_assigned_siblings(tmp_path):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    registry.update_image('sample-0', split=None, session_id='session-2')
    original = [dict(row) for row in registry.conn.execute('SELECT * FROM annotations')]
    registry.close()
    assert run(settings)['repaired'] == 1
    registry = Registry(settings.db_path)
    assert registry.image('sample-0')['split'] == 'test'
    assert registry.image('sample-1')['split'] == 'val'
    assert registry.image('sample-2')['split'] == 'test'
    assert [dict(row) for row in registry.conn.execute('SELECT * FROM annotations')] == original
    registry.close()
    assert run(settings)['repaired'] == 0


def test_new_recording_gets_the_existing_recovery_extension_policy(tmp_path):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    registry.update_image('sample-0', split=None)
    registry.close()
    run(settings)
    registry = Registry(settings.db_path)
    assert registry.image('sample-0')['split'] == split_for('session-0')
    registry.close()


def test_frozen_reserved_recording_applies_to_all_unassigned_siblings(tmp_path):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    for image_id in ('sample-0', 'sample-1'):
        registry.update_image(image_id, split=None, session_id='new-recording')
    registry.close()
    frozen = settings.path('models', 'ufld_baseline', 'prior-run', 'dataset')
    frozen.mkdir(parents=True)
    (frozen / 'reserved_test_ids.json').write_text(json.dumps(['sample-1']))
    run(settings)
    registry = Registry(settings.db_path)
    assert registry.image('sample-0')['split'] == registry.image('sample-1')['split'] == 'test'
    registry.close()


def test_conflicting_frozen_and_live_session_assignments_abort(tmp_path):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    registry.update_image('sample-1', split=None, session_id='session-0')
    registry.close()
    frozen = settings.path('models', 'ufld_baseline', 'prior-run', 'dataset')
    frozen.mkdir(parents=True)
    (frozen / 'reserved_test_ids.json').write_text(json.dumps(['sample-1']))
    with pytest.raises(ValueError, match='conflict'):
        run(settings)
    registry = Registry(settings.db_path)
    assert registry.image('sample-0')['split'] == 'train'
    assert registry.image('sample-1')['split'] is None
    registry.close()


def test_unassigned_photo_can_be_reviewed_but_cannot_be_exported(tmp_path):
    settings = build_review_fixture(tmp_path)
    registry = Registry(settings.db_path)
    registry.update_image('sample-0', split=None)
    registry.close()
    store = ReviewStore(settings)
    decide(store)
    decide(store, image_id='sample-1')
    report = export_reviewed(settings)
    assert report['exported'] == 1
    assert report['held_missing_split_ids'] == ['sample-0']
    assert store.detail('sample-0')['review_status'] == 'approved'
    assert settings.path('reviewed', 'human_approved', 'unassigned', 'sample-0.json').is_file()
