import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from brakecheck_dataset.pipeline import (
    comma10k_plan, fetch_file, git_blob_sha1, import_pair, require_clearance, setup, split_for,
)
from brakecheck_dataset.video import extract_video
from brakecheck_dataset.review import overlap
from brakecheck_dataset.gallery import build_gallery, human_overlay


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config/sources.json").write_text(json.dumps({
            "comma10k": {"pool": "commercial", "license_status": "cleared", "media": "images"},
            "licensed_video": {"pool": "commercial", "license_status": "cleared", "media": "video"},
            "unclear": {"pool": "quarantine", "license_status": "review_required", "media": "video"},
        }))

    def tearDown(self):
        self.temp.cleanup()

    def test_unlicensed_source_cannot_enter_commercial_pool(self):
        with self.assertRaises(PermissionError):
            require_clearance(self.root, "unclear")
        with self.assertRaises(PermissionError):
            require_clearance(self.root, "missing")

    def test_plan_pins_commit_pairs_and_inventory_size(self):
        tree = {"truncated": False, "tree": [
            {"path": f"imgs/{i}.png", "type": "blob", "sha": str(i), "size": 10}
            for i in range(5)
        ] + [
            {"path": f"masks/{i}.png", "type": "blob", "sha": str(i + 5), "size": 3}
            for i in range(5)
        ]}
        with patch("brakecheck_dataset.pipeline.github_json", side_effect=[{"sha": "commit"}, tree]):
            plan = comma10k_plan(self.root, self.root, 3)
        self.assertEqual(plan["commit"], "commit")
        self.assertEqual(plan["total_bytes"], 39)
        self.assertEqual([entry["image"] for entry in plan["pairs"]],
                         ["imgs/0.png", "imgs/2.png", "imgs/4.png"])

    def test_import_is_idempotent_and_preserves_original_mask(self):
        db = setup(self.root)
        image_path = self.root / "raw/comma10k/imgs/0001_route_2026.png"
        mask_path = self.root / "raw/comma10k/masks/0001_route_2026.png"
        image_path.parent.mkdir(parents=True)
        mask_path.parent.mkdir(parents=True)
        Image.new("RGB", (64, 32), (90, 100, 110)).save(image_path)
        Image.new("RGB", (64, 32), (255, 0, 0)).save(mask_path)
        original_mask = hashlib.sha256(mask_path.read_bytes()).hexdigest()
        pair = {"image": "imgs/0001_route_2026.png", "mask": "masks/0001_route_2026.png"}
        first = import_pair(db, self.root, pair, "commit")
        second = import_pair(db, self.root, pair, "commit")
        self.assertEqual(first, second)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
        self.assertEqual(hashlib.sha256(mask_path.read_bytes()).hexdigest(), original_mask)
        self.assertEqual(db.execute("SELECT mask_sha256 FROM images").fetchone()[0], original_mask)
        db.close()

    def test_split_is_stable_by_session_and_git_blob_hash_matches(self):
        self.assertEqual(split_for("comma10k", "one"), split_for("comma10k", "one"))
        self.assertEqual(git_blob_sha1(b"test"), hashlib.sha1(b"blob 4\0test").hexdigest())

    def test_download_rejects_path_traversal_before_network(self):
        with self.assertRaises(ValueError):
            fetch_file("commit", "imgs/../../outside.png", self.root / "outside.png", "hash", 10)

    def test_video_extraction_is_rights_gated_and_idempotent(self):
        video = self.root / "sample.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 4, (64, 48))
        self.assertTrue(writer.isOpened())
        for index in range(16):
            frame = np.full((48, 64, 3), index * 8, dtype=np.uint8)
            writer.write(frame)
        writer.release()
        with self.assertRaises(PermissionError):
            extract_video(self.root, self.root, "unclear", video, "session")
        with self.assertRaises(PermissionError):
            extract_video(self.root, self.root, "comma10k", video, "session")
        first = extract_video(self.root, self.root, "licensed_video", video, "session")
        second = extract_video(self.root, self.root, "licensed_video", video, "session")
        self.assertGreaterEqual(first["selected"], 4)
        self.assertEqual(first["selected"], second["selected"])
        db = sqlite3.connect(self.root / "metadata.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM images").fetchone()[0], first["selected"])
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT split) FROM images").fetchone()[0], 1)
        finally:
            db.close()

    def test_overlap_counts_false_positives_and_misses(self):
        predicted = np.array([[1, 1], [0, 0]], dtype=bool)
        source = np.array([[1, 0], [1, 0]], dtype=bool)
        result = overlap(predicted, source)
        self.assertEqual(result["iou"], 1 / 3)
        self.assertEqual(result["precision"], .5)
        self.assertEqual(result["recall"], .5)

    def test_gallery_shows_color_photo_with_separate_overlays(self):
        image_id = "abc123"
        original = self.root / "raw/comma10k/imgs/photo.png"
        mask = self.root / "raw/comma10k/masks/photo.png"
        overlay = self.root / "annotations/teacher_predictions/comma10k-segnet/abc123-overlay.jpg"
        for path in (original, mask, overlay):
            path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (20, 90, 150)).save(original)
        Image.new("RGB", (64, 48), (255, 0, 0)).save(mask)
        Image.new("RGB", (64, 48), (20, 90, 150)).save(overlay)
        self.assertNotEqual(human_overlay(Image.open(original), Image.open(mask)).getpixel((10, 10)), (0, 0, 0))
        queue = self.root / "review_queue/comma-source-disagreements.jsonl"
        queue.parent.mkdir(parents=True)
        queue.write_text(json.dumps({"image_id": image_id,
            "image": "raw/comma10k/imgs/photo.png", "original_mask": "raw/comma10k/masks/photo.png",
            "priority": "high", "lane": {"iou": .3}}) + "\n")
        result = build_gallery(self.root)
        self.assertEqual(result["cards"], 1)
        page = (self.root / "review_gallery/index.html").read_text()
        self.assertIn("Original photo", page)
        self.assertIn("White-on-black PNGs are machine-readable masks", page)
        self.assertTrue((self.root / "review_gallery/cards/abc123.jpg").is_file())


if __name__ == "__main__":
    unittest.main()

