"""Load only boundary-relevant source classes with the shared palette tolerance."""
import json
from pathlib import Path

import numpy as np

from .quality import read_gray_u8, read_rgb
from .sources.comma10k import CLASS_COLORS, LANE, MOVABLE, MY_CAR


def maps_from_rgb(rgb):
    pixels = np.asarray(rgb, dtype=np.int16)
    maps = {}
    for name, cls in (("lane", LANE), ("my_car", MY_CAR), ("movable", MOVABLE)):
        red, green, blue = CLASS_COLORS[cls]
        distance = (np.abs(pixels[:, :, 0] - red) + np.abs(pixels[:, :, 1] - green)
                    + np.abs(pixels[:, :, 2] - blue))
        maps[name] = (distance <= 24).astype(np.float32)
    return maps


def load_boundary_maps(annotation):
    params = json.loads(annotation['params'] or '{}')
    fmt = params.get('format')
    if fmt == 'comma10k_rgb':
        rgb = read_rgb(annotation['path'])
        if rgb is None:
            raise OSError(annotation['path'])
        return maps_from_rgb(rgb)
    if fmt == 'a2d2_rgb':
        from .sources.a2d2 import mask_to_classes
        rgb = read_rgb(annotation['path'])
        if rgb is None:
            raise OSError(annotation['path'])
        palette = json.loads(Path(params['palette_path']).read_text(encoding='utf-8'))
        classes = mask_to_classes(rgb, palette=palette)
        maps = {name:(classes == cls).astype(np.float32)
                for name,cls in (("lane",LANE),("my_car",MY_CAR),("movable",MOVABLE))}
        # Keep the original Audi paint types for source-only association.
        # A semantic type is evidence for choosing between nearby fragments;
        # it never makes arrows, road pixels or transverse paint into lanes.
        for name, label in (("solid", "Solid line"), ("dashed", "Dashed line")):
            selected = np.zeros(classes.shape, dtype=bool)
            for color, description in palette.items():
                if description == label:
                    rgb_color = tuple(int(color[index:index+2], 16) for index in (1, 3, 5))
                    selected |= np.all(rgb == rgb_color, axis=-1)
            maps[name] = selected.astype(np.float32)
        return maps
    if fmt == 'class_map':
        classes = read_gray_u8(annotation['path'])
        if classes is None:
            raise OSError(annotation['path'])
        return {name:(classes == cls).astype(np.float32)
                for name,cls in (("lane",LANE),("my_car",MY_CAR),("movable",MOVABLE))}
    raise ValueError('Unsupported source label format: ' + str(fmt))
