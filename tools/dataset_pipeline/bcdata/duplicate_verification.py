"""Verify a hash-similarity candidate using capture identity and actual pixels.

Perceptual hashes only find candidates. They cannot exclude another road,
another recording, or another camera merely because its composition is alike.
"""
from pathlib import Path

import cv2
import numpy as np

from .quality import read_rgb

POLICY = 'same-capture-pixel-duplicate-v1'


def ssim(a, b):
    a, b = a.astype(np.float32), b.astype(np.float32)
    mu_a, mu_b = cv2.GaussianBlur(a, (11, 11), 1.5), cv2.GaussianBlur(b, (11, 11), 1.5)
    aa, bb, ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    va = cv2.GaussianBlur(a * a, (11, 11), 1.5) - aa
    vb = cv2.GaussianBlur(b * b, (11, 11), 1.5) - bb
    cov = cv2.GaussianBlur(a * b, (11, 11), 1.5) - ab
    value = ((2 * ab + 6.5025) * (2 * cov + 58.5225)) / ((aa + bb + 6.5025) * (va + vb + 58.5225))
    return float(value.mean())


def verify_pair(first, second):
    evidence = {'policy': POLICY, 'first': first['image_id'], 'second': second['image_id']}
    if first['sha256'] == second['sha256']:
        return {**evidence, 'duplicate': True, 'reason': 'same_content_sha256'}
    for key in ('source', 'session_id', 'width', 'height'):
        if first[key] != second[key]:
            return {**evidence, 'duplicate': False, 'reason': 'different_' + key}
    frames = first['frame_index'], second['frame_index']
    if None in frames or abs(frames[0] - frames[1]) > 30:
        return {**evidence, 'duplicate': False, 'reason': 'not_a_nearby_capture'}
    a, b = read_rgb(first['path']), read_rgb(second['path'])
    if a is None or b is None or a.shape != b.shape:
        raise ValueError('Duplicate comparison requires two readable native images of matching size')
    a, b = (cv2.resize(image, (320, 200), interpolation=cv2.INTER_AREA) for image in (a, b))
    gray_a, gray_b = (cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) for image in (a, b))
    road_a, road_b = gray_a[90:190], gray_b[90:190]
    metrics = {'full_ssim': ssim(gray_a, gray_b), 'road_ssim': ssim(road_a, road_b),
               'rgb_mae': float(np.abs(a.astype(float) - b.astype(float)).mean()),
               'road_mae': float(np.abs(road_a.astype(float) - road_b.astype(float)).mean())}
    duplicate = (metrics['full_ssim'] >= .99 and metrics['road_ssim'] >= .99
                 and metrics['rgb_mae'] <= 2 and metrics['road_mae'] <= 2)
    return {**evidence, **metrics, 'duplicate': duplicate,
            'reason': 'verified_nearby_pixel_duplicate' if duplicate else 'distinct_native_image'}
