from copy import deepcopy

import numpy as np
from PIL import Image

from bcdata.duplicate_verification import verify_pair


def rows(tmp_path):
    rng = np.random.default_rng(9)
    pixels = rng.integers(0, 256, (200, 320, 3), dtype=np.uint8)
    result = []
    for index in range(2):
        path = tmp_path / f'frame-{index}.png'
        Image.fromarray(pixels).save(path)
        result.append({'image_id': str(index), 'sha256': str(index), 'source': 'a2d2',
                       'session_id': 'recording', 'width': 320, 'height': 200,
                       'frame_index': index, 'path': str(path)})
    return result


def test_matching_nearby_capture_requires_strong_actual_pixel_agreement(tmp_path):
    first, second = rows(tmp_path)
    assert verify_pair(first, second)['duplicate']
    Image.fromarray(np.zeros((200, 320, 3), np.uint8)).save(second['path'])
    assert not verify_pair(first, second)['duplicate']


def test_same_composition_from_another_camera_or_recording_is_retained(tmp_path):
    first, second = rows(tmp_path)
    for key, value in [('source', 'comma10k'), ('session_id', 'another-recording'), ('frame_index', 1000)]:
        changed = deepcopy(second)
        changed[key] = value
        assert not verify_pair(first, changed)['duplicate']


def test_actual_content_hash_duplicates_remain_duplicates_globally(tmp_path):
    first, second = rows(tmp_path)
    second.update(source='other', session_id='another', sha256=first['sha256'])
    assert verify_pair(first, second)['duplicate']


def test_changed_road_content_is_not_hidden_by_similar_sky(tmp_path):
    first, second = rows(tmp_path)
    a = np.full((200, 320, 3), 150, np.uint8)
    b = a.copy()
    b[135:160, 100:120] = (255, 255, 255)
    Image.fromarray(a).save(first['path'])
    Image.fromarray(b).save(second['path'])
    assert not verify_pair(first, second)['duplicate']
