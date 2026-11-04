from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from bcdata.db import Registry
from bcdata.recover_dropped_images import publish
from bcdata.recovery_source_maps import classes_and_maps
from bcdata.review_store import ReviewStore
from bcdata.teachers.base import file_sha256
from bcdata.teachers.source_labels import load_classes
from tests.test_review import build_review_fixture


def test_fast_decoder_matches_official_decoder_on_all_palette_colors(tmp_path):
    palette = {'#ffc125': 'Solid line', '#8000ff': 'Dashed line', '#ff00ff': 'RD normal street',
               '#ff0000': 'Car 1', '#87ceff': 'Ego car', '#c87dd2': 'Painted driv. instr.',
               '#0000ff': 'Non-drivable street', '#123456': 'Pedestrian 2'}
    rng = np.random.default_rng(82)
    pixels = rng.integers(0, 256, (50, 100, 3), dtype=np.uint8)
    for index, color in enumerate(palette):
        pixels[index] = tuple(int(color[offset:offset + 2], 16) for offset in (1, 3, 5))
    path, palette_path = tmp_path / 'mask.png', tmp_path / 'palette.json'
    Image.fromarray(pixels).save(path)
    palette_path.write_text(json.dumps(palette))
    annotation = {'path': str(path), 'params': json.dumps({'format': 'a2d2_rgb', 'palette_path': str(palette_path)})}
    classes, maps = classes_and_maps(annotation)
    assert np.array_equal(classes, load_classes(annotation))
    assert np.array_equal(maps['lane'], classes == 2)
    assert np.array_equal(maps['solid'] | maps['dashed'], classes == 2)


def recovery_fixture(tmp_path):
    settings = build_review_fixture(tmp_path)
    store = ReviewStore(settings)
    records = []
    with store.registry() as reg:
        for index in range(3):
            image_id = f'sample-{index}'
            source_path = tmp_path / (image_id + '-mask.png')
            Image.fromarray(np.ones((874, 1164), np.uint8)).save(source_path)
            reg.add_annotation(image_id, 'source_label', 'comma10k', 'fixture-source', str(source_path),
                               params={'human': True, 'format': 'class_map'})
            reg.update_image(image_id, status='dropped', reject_reason='near_duplicate_expansion')
            image = dict(reg.image(image_id))
            # The production verifier compares this genuine file fingerprint.
            reg.update_image(image_id, sha256=file_sha256(image['path']))
            image = dict(reg.image(image_id))
            geometry, source = dict(reg.annotation(image_id, 'geometry', 'geometry')), dict(reg.annotation(image_id, 'source_label'))
            teacher_path = tmp_path / (image_id + '-comparison.json')
            teacher_path.write_text('{}')
            records.append({'image_id': image_id, 'previous_image': image, 'source': source, 'geometry': geometry,
                            'source_sha256': file_sha256(source_path), 'geometry_sha256': file_sha256(geometry['path']),
                            'teacher_path': str(teacher_path), 'teacher_sha256': file_sha256(teacher_path),
                            'quality': dict(reg.conn.execute('SELECT * FROM qc WHERE image_id=?', (image_id,)).fetchone()),
                            'reasons': [], 'audit_risks': [], 'stratum': image['split'] + ':day:lanes-1',
                            'duplicate_comparison': {'duplicate': False, 'reason': 'distinct_native_image'}})
    directory = tmp_path / 'reports' / 'recovery'
    directory.mkdir()
    return settings, store, directory, records


def test_recovered_images_need_real_audits_before_training(tmp_path):
    settings, store, directory, records = recovery_fixture(tmp_path)
    result = publish(settings, directory, records)
    assert result['restored'] == 3 and result['counts'] == {'sample': 3}
    assert result['training_held'] and result['human_decisions_created'] == 0
    with store.registry() as reg:
        assert reg.count('SELECT count(*) FROM review_events') == 0
        assert reg.count('SELECT count(*) FROM reviews') == 0
        assert reg.count("SELECT count(*) FROM source_conversion_checks WHERE state='validated'") == 0
        assert reg.count('SELECT count(*) FROM dropped_image_recovery_history') == 3


def test_changed_native_source_aborts_without_restoring_any_image(tmp_path):
    settings, store, directory, records = recovery_fixture(tmp_path)
    Path(records[-1]['source']['path']).write_bytes(b'changed mask')
    with pytest.raises(ValueError, match='annotation changed'):
        publish(settings, directory, records)
    with store.registry() as reg:
        assert reg.count("SELECT count(*) FROM images WHERE status='dropped'") == 3
        assert reg.count('SELECT count(*) FROM source_conversion_checks') == 0


def test_recovery_assigns_an_unassigned_recording_without_moving_existing_splits(tmp_path):
    settings, store, directory, records = recovery_fixture(tmp_path)
    with store.registry() as reg:
        reg.update_image('sample-0', split=None, has_source_label=0)
        records[0]['previous_image'] = dict(reg.image('sample-0'))
    records[0]['assigned_split'] = 'train'
    records[0]['stratum'] = 'train:day:lanes-1'
    result = publish(settings, directory, records)
    assert result['restored'] == 3
    with store.registry() as reg:
        assert reg.image('sample-0')['split'] == 'train'
        assert reg.image('sample-0')['has_source_label'] == 1
        assert reg.image('sample-1')['split'] == 'val'
        assert reg.image('sample-2')['split'] == 'test'
