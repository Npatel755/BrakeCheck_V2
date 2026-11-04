"""Exact source classes for recovery, with a cached packed-RGB lookup."""
from functools import lru_cache
import json
from pathlib import Path
import re

import numpy as np

from .quality import read_rgb
from .teachers.source_labels import load_classes
from .teachers.base import Teacher, TeacherOutput


@lru_cache(maxsize=4)
def _lookup(items):
    table = np.zeros(1 << 24, np.uint8)
    for color, cls in ((0xff00ff, 1), (0xffc125, 2), (0x8000ff, 2)):
        table[color] = cls
    for color, description in items:
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            continue
        name = str(description).strip().casefold()
        cls = (5 if name == 'ego car' else 4 if re.fullmatch(
            r'(?:car|truck|small vehicles|pedestrian|bicycle)\s*\d*', name)
            else 3 if name == 'non-drivable street' else 0)
        if cls:
            table[int(color[1:], 16)] = cls
    return table


def classes_and_maps(annotation):
    params = json.loads(annotation['params'] or '{}')
    solid = dashed = None
    if params.get('format') == 'a2d2_rgb':
        rgb = read_rgb(annotation['path'])
        if rgb is None:
            raise ValueError('Unreadable source mask')
        palette = json.loads(Path(params['palette_path']).read_text(encoding='utf-8'))
        values = rgb.astype(np.uint32)
        packed = (values[:, :, 0] << 16) | (values[:, :, 1] << 8) | values[:, :, 2]
        classes = _lookup(tuple(palette.items()))[packed]
        solid, dashed = packed == 0xffc125, packed == 0x8000ff
    else:
        classes = load_classes(annotation)
    maps = {name: (classes == cls).astype(np.float32)
            for name, cls in (('lane', 2), ('movable', 4), ('my_car', 5))}
    maps.update(solid=solid, dashed=dashed)
    return classes, maps


class RecoverySourceTeacher(Teacher):
    name = 'source_labels'
    license_ids = []

    def __init__(self, settings, registry):
        super().__init__(settings)
        self.registry = registry

    @property
    def version(self):
        return 'source_labels:exact-packed-v2'

    def predict(self, rgb, row):
        annotation = self.registry.annotation(row['image_id'], 'source_label')
        classes, maps = classes_and_maps(annotation)
        if classes.shape != rgb.shape[:2]:
            raise ValueError('Source dimensions do not match the original image')
        return TeacherOutput(maps['lane'], ((classes == 1) | (classes == 2)).astype(np.float32),
                             {name: maps[name] for name in ('movable', 'my_car') if maps[name].any()},
                             {'producer': annotation['producer'], 'label_version': annotation['model_version'],
                              'human': json.loads(annotation['params']).get('human', False),
                              'decoder': 'exact-packed-RGB; original source bytes preserved'})
