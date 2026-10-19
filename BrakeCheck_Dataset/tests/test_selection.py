from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from brakecheck_dataset.selection import (
    DESCRIPTOR_VERSION, InvalidImageError, analyze_image, select_frames,
)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def image(self, name, pixels, *, image_id=None, source="licensed-video",
              session="capture-1", split="train", **save_options):
        path = self.root / name
        Image.fromarray(pixels.astype(np.uint8), "RGB").save(path, **save_options)
        return analyze_image(path, image_id=image_id or path.stem, source=source,
                             session_id=session, split=split)

    def test_corrupt_and_too_small_images_are_rejected(self):
        broken = self.root / "broken.jpg"
        broken.write_bytes(b"not an image")
        with self.assertRaises(InvalidImageError):
            analyze_image(broken, image_id="bad", source="licensed-video",
                          session_id="capture-1", split="train")
        tiny = np.zeros((16, 16, 3), dtype=np.uint8)
        with self.assertRaises(InvalidImageError):
            self.image("tiny.png", tiny)

    def test_dark_and_low_detail_images_are_flagged_but_retained(self):
        night = self.image("night.png", np.full((96, 128, 3), 8, dtype=np.uint8))
        self.assertIn("dark_review", night.quality_flags)
        self.assertIn("blur_review", night.quality_flags)
        self.assertEqual(select_frames([night]).selected_ids, ("night",))
        self.assertEqual(night.descriptor_version, DESCRIPTOR_VERSION)

    def test_exact_duplicates_across_splits_are_removed(self):
        pixels = np.full((96, 128, 3), 140, dtype=np.uint8)
        original = self.image("original.png", pixels, image_id="a", session="one",
                              split="train")
        copy = self.image("copy.png", pixels, image_id="b", session="two",
                          split="test")
        result = select_frames([copy, original])
        self.assertEqual(result.selected_ids, ("a",))
        self.assertEqual(result.exact_duplicates, {"b": "a"})

    def test_same_pixels_with_different_file_encoding_are_near_duplicates(self):
        y, x = np.mgrid[:96, :128]
        pixels = np.zeros((96, 128, 3), dtype=np.uint8)
        pixels[..., 0] = x * 2
        pixels[..., 1] = y * 2
        pixels[..., 2] = np.where(x > 50 + y // 5, 240, 40)
        first = self.image("first.png", pixels, image_id="a", compress_level=0)
        second = self.image("second.png", pixels, image_id="b", compress_level=9)
        self.assertNotEqual(first.file_sha256, second.file_sha256)
        result = select_frames([second, first])
        self.assertEqual(result.selected_ids, ("a",))
        self.assertEqual(result.near_duplicates, {"b": "a"})

    def test_similar_phashes_do_not_collapse_different_lighting(self):
        dark = self.image("dark.png", np.full((96, 128, 3), 20, dtype=np.uint8),
                          image_id="dark")
        bright = self.image("bright.png", np.full((96, 128, 3), 220, dtype=np.uint8),
                            image_id="bright")
        self.assertEqual(dark.phash, bright.phash)
        result = select_frames([dark, bright], n_clusters=2)
        self.assertEqual(set(result.selected_ids), {"dark", "bright"})

    def test_cluster_budgets_are_per_split_and_deterministic(self):
        frames = []
        for index, (split, session) in enumerate((
            ("train", "train-a"), ("train", "train-b"),
            ("validation", "val-a"), ("validation", "val-b"),
            ("test", "test-a"), ("test", "test-b"),
        )):
            pixels = np.zeros((96, 128, 3), dtype=np.uint8)
            pixels[..., index % 3] = 60 + index * 24
            pixels[:, :10 + index * 10, :] = 255 - index * 15
            frames.append(self.image(f"{index}.png", pixels, image_id=f"id-{index}",
                                     session=session, split=split))
        first = select_frames(frames, n_clusters=1, per_cluster=1)
        second = select_frames(list(reversed(frames)), n_clusters=1, per_cluster=1)
        self.assertEqual(first, second)
        self.assertEqual(len(first.selected_ids), 3)
        self.assertEqual(len(first.cluster_overflow), 3)
        self.assertEqual({label.split(":")[0] for label in first.cluster_by_id.values()},
                         {"train", "validation", "test"})

    def test_session_cannot_span_training_and_evaluation(self):
        pixels = np.zeros((96, 128, 3), dtype=np.uint8)
        first = self.image("first.png", pixels, image_id="first")
        second = replace(first, image_id="second", split="test")
        with self.assertRaisesRegex(ValueError, "session spans multiple splits"):
            select_frames([first, second])

    def test_duplicate_ids_and_mixed_descriptor_versions_are_rejected(self):
        pixels = np.zeros((96, 128, 3), dtype=np.uint8)
        frame = self.image("frame.png", pixels)
        with self.assertRaisesRegex(ValueError, "Duplicate image IDs"):
            select_frames([frame, frame])
        with self.assertRaisesRegex(ValueError, "Mixed or unsupported"):
            select_frames([replace(frame, descriptor_version="other")])


if __name__ == "__main__":
    unittest.main()
