"""Explicit source sampling and conservative short-trace geometry checks."""
from collections import Counter
import numpy as np

STRICT_GEOMETRY_RULES = {'max_normalized_step': .15, 'max_path_to_chord': 1.35,
                         'max_turn_degrees': 60., 'minimum_points': 4}
GEOMETRY_RULES = {'max_normalized_step': .15, 'max_path_to_chord': 2.,
                  'max_turn_degrees': 150., 'minimum_points': 4}


def credible_trace(points, rules=None):
    rules = rules or GEOMETRY_RULES
    points = np.asarray(points, dtype=np.float64) / [1640., 590.]
    if points.shape[0] < rules['minimum_points'] or not np.isfinite(points).all():
        return False
    delta = np.diff(points, axis=0)
    lengths = np.linalg.norm(delta, axis=1)
    chord = np.linalg.norm(points[-1] - points[0])
    if (chord <= 1e-6 or lengths.max() > rules['max_normalized_step']
            or lengths.sum() / chord > rules['max_path_to_chord']):
        return False
    vectors = delta[lengths > 1e-6]
    if len(vectors) > 1:
        cosine = (vectors[:-1] * vectors[1:]).sum(axis=1) / (
            np.linalg.norm(vectors[:-1], axis=1) * np.linalg.norm(vectors[1:], axis=1))
        if np.degrees(np.arccos(np.clip(cosine, -1, 1))).max() > rules['max_turn_degrees']:
            return False
    return True


def source_sampling(records, mode):
    if mode not in {'uniform', 'balanced'}:
        raise ValueError('Unknown source sampling mode')
    if not records or any(row['split'] != 'train' for row in records):
        raise ValueError('Sampling may use nonempty TRAIN records only')
    counts = Counter(row['source'] for row in records)
    weights = [1. / counts[row['source']] for row in records] if mode == 'balanced' else None
    return weights, {'mode': mode, 'source_counts': dict(counts),
                     'expected_source_fractions': {source: 1 / len(counts) if weights else count / len(records)
                                                   for source, count in counts.items()},
                     'samples_per_epoch': len(records), 'replacement': mode == 'balanced',
                     'seed_policy': 'epoch index; deterministic epoch restart'}
