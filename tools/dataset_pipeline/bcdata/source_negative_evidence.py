"""Prove a zero-boundary target from empty or exclusively transverse paint."""
import cv2
import numpy as np

VERSION = 'native-source-negative-v1'


def measure(geometry, probability):
    if any(line.get('slot') for line in geometry.get('lanes', [])):
        return None  # This evidence cannot remove an existing boundary.
    paint = np.asarray(probability) >= .5
    paint = paint.copy()
    paint[:geometry['horizon_y']] = False
    paint[geometry['valid_bottom'] + 1:] = False
    count, labels, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), 8)
    transverse = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if area <= 3:
            continue
        # Even tiny vertical dashes remain unresolved. Only an actual broad,
        # nearly horizontal stripe can be dismissed as transverse paint.
        if height > max(2., .08 * width) or width < 12:
            return None
        rows, cols = np.nonzero(labels[y:y + height, x:x + width] == index)
        _, singular, direction = np.linalg.svd(np.column_stack((cols - cols.mean(), rows - rows.mean())), full_matrices=False)
        if not singular.size or abs(direction[0, 1]) > .08:
            return None
        transverse.append({'bbox': [x, y, width, height], 'pixels': area,
                           'absolute_vertical_direction': float(abs(direction[0, 1]))})
    return {'version': VERSION, 'family': 'transverse-only' if transverse else 'empty-source',
            'source_paint_pixels': int(paint.sum()), 'transverse_components': transverse,
            'source_only': True, 'requires_fresh_human_audit': True}
