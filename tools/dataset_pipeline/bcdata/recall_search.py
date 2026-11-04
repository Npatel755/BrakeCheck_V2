"""Decoder-only recall selection with an explicit false-positive budget."""
from itertools import product
import json
import math
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment

from .lane_improvements import credible_trace, GEOMETRY_RULES


def candidate_heads(pred, index):
    """Cache unfiltered model geometry and confidence; never use reference labels."""
    result = []
    for axis, anchors, extent in (('row', np.linspace(.42, 1, 72), 1640), ('col', np.linspace(0, 1, 81), 590)):
        logits = pred['loc_' + axis][index].float().cpu()
        existence = pred['exist_' + axis][index].float().cpu()
        valid = existence.argmax(0).numpy().astype(bool)
        centers = logits.argmax(0)
        for slot in range(4):
            selected = valid[:, slot]
            probability = existence[:, selected, slot].softmax(0)[1].mean().item() if selected.any() else 0.
            points = []
            for anchor_index in np.flatnonzero(selected):
                center = int(centers[anchor_index, slot])
                start, stop = max(0, center - 1), min(logits.shape[0], center + 2)
                scores = logits[start:stop, anchor_index, slot].softmax(0).numpy()
                coord = (np.dot(scores, np.arange(start, stop)) + .5) / (logits.shape[0] - 1) * extent
                point = [coord, anchors[anchor_index] * 590] if axis == 'row' else [anchors[anchor_index] * 1640, coord]
                if 0 <= point[0] < 1640 and 0 <= point[1] < 590:
                    points.append([float(v) for v in point])
            result.append({'axis': axis, 'slot': slot, 'positive_anchors': int(selected.sum()),
                           'mean_confidence': float(probability), 'points': points})
    return result


STOCK_RULE = {'row_minimum': 37, 'col_minimum': 21, 'confidence': 0., 'geometry': False, 'preserve_stock': False}
SHORT_RULE = {'row_minimum': 4, 'col_minimum': 4, 'confidence': .75, 'geometry': False, 'preserve_stock': False}


def select_indices(candidates, rule):
    selected = []
    for i, candidate in enumerate(candidates):
        axis, slot = candidate['axis'], candidate['slot']
        if (axis == 'row' and slot not in (1, 2)) or (axis == 'col' and slot not in (0, 3)):
            continue
        if (candidate['positive_anchors'] < rule[axis + '_minimum'] or len(candidate['points']) < 2
                or candidate['mean_confidence'] < rule['confidence']):
            continue
        if rule.get('geometry') and not credible_trace(candidate['points'], rule.get('geometry_rules', GEOMETRY_RULES)):
            continue
        selected.append(i)
    if rule.get('preserve_stock'):
        selected = sorted(set(selected) | set(select_indices(candidates, STOCK_RULE)))
    return selected


def search_rules():
    # Fixed before inspecting results: 80 candidates; no test-set tuning.
    return [dict(row_minimum=minimum, col_minimum=minimum, confidence=confidence,
                 geometry=geometry, preserve_stock=preserve)
            for minimum, confidence, geometry, preserve in product((2, 4, 8, 12), (.6, .7, .75, .8, .85), (False, True), (False, True))]


def score_matrix(matrix, selected):
    matrix = np.asarray(matrix, dtype=np.float32)
    refs = matrix.shape[1]
    tp = 0
    if selected and refs:
        values = matrix[selected]
        a, b = linear_sum_assignment(-values)
        tp = int((values[a, b] >= .5).sum())
    return {'tp': tp, 'fp': len(selected) - tp, 'fn': refs - tp}


def choose_with_budget(results, fp_budget):
    eligible = [row for row in results if row['fp'] <= fp_budget]
    return max(eligible, key=lambda row: (row['recall'], -row['fp'], row['lane_f1'])) if eligible else None


def load_policy(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    rule = config['rule']
    for key, maximum in (('row_minimum',72),('col_minimum',81)):
        if type(rule.get(key)) is not int or not 2 <= rule[key] <= maximum:
            raise ValueError('Invalid recall decoder anchor threshold')
    confidence = rule.get('confidence')
    if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('Invalid recall decoder confidence threshold')
    if any(type(rule.get(key)) is not bool for key in ('geometry','preserve_stock')):
        raise ValueError('Recall decoder flags must be booleans')
    if type(config.get('false_positive_budget')) is not int or config['false_positive_budget'] < 0:
        raise ValueError('Invalid recall false-positive budget')
    if config.get('test_images_used') != 0 or not isinstance(config.get('manifest_sha256'),str):
        raise ValueError('Recall decoder must be bound to validation-only evidence')
    return config
