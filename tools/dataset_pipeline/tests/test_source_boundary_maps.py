import numpy as np

from bcdata.source_boundary_maps import maps_from_rgb
from bcdata.sources.comma10k import CLASS_COLORS, mask_to_classes


def test_boundary_maps_match_shared_taxonomy_and_palette_tolerance():
    random = np.random.default_rng(719)
    palette = np.asarray(list(CLASS_COLORS.values()), dtype=np.int16)
    noisy = np.clip(palette[:,None,:] + random.integers(-26,27,size=(6,100,3)),0,255).astype(np.uint8)
    rgb = np.concatenate([noisy,random.integers(0,256,size=(6,100,3),dtype=np.uint8)],axis=1)
    classes = mask_to_classes(rgb)
    for name,cls in (("lane",2),("my_car",5),("movable",4)):
        np.testing.assert_array_equal(maps_from_rgb(rgb)[name],(classes==cls).astype(np.float32))
