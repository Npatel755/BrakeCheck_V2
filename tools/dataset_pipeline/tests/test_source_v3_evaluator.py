"""Development evidence must never manufacture or alter actual reviews."""
import json
from pathlib import Path
import sqlite3

import cv2
import numpy as np
import pytest

from tools.evaluate_source_v3 import evaluate, observed_extents, sha256, support


def test_support_excludes_unobserved_gap_points_and_tracks_lost_extents():
    before = {"lanes": [{"slot": 2, "points": [[10, 10, True], [10, 20, False], [10, 30, True]]}]}
    after = {"lanes": [{"slot": 2, "points": [[10, 10, True], [10, 20, True]]}]}
    paint = np.zeros((40, 40), np.float32)
    paint[10, 10] = paint[30, 10] = 1
    result = support(before, paint)
    assert result["observed"] == result["supported"] == 2
    assert result["fraction"] == 1
    extent = observed_extents(before, after)["2"]
    assert extent["retained_span_fraction"] == .5
    assert extent["preserved_with_2px_tolerance"] is False


def fixture_database(tmp_path):
    image_id = "fixture-source-sample"
    geometry = {"image_id": image_id, "width": 100, "height": 100, "horizon_y": 20,
                "valid_bottom": 90, "lanes": [{"slot": 2, "points": [[20, 20, True], [40, 80, True]],
                                               "curvature": 0., "gaps": []}]}
    geometry_path, human_path, source_path = (tmp_path / name for name in ("geometry.json", "human.json", "source.png"))
    geometry_path.write_text(json.dumps(geometry), encoding="utf-8")
    human_path.write_text(json.dumps(geometry), encoding="utf-8")
    classes = np.ones((100, 100), np.uint8)
    cv2.line(classes, (20, 20), (40, 80), 2, 3)
    assert cv2.imwrite(str(source_path), classes)
    database = tmp_path / "metadata.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE images(image_id TEXT PRIMARY KEY,review_status TEXT,locked INTEGER,split TEXT);
        CREATE TABLE source_validation_groups(group_key TEXT PRIMARY KEY,batch_id TEXT,stratum TEXT,sample_ids TEXT);
        CREATE TABLE source_conversion_checks(image_id TEXT PRIMARY KEY,group_key TEXT,source_id INTEGER,
          source_sha256 TEXT,geometry_path TEXT,geometry_sha256 TEXT);
        CREATE TABLE annotations(id INTEGER PRIMARY KEY,image_id TEXT,kind TEXT,path TEXT,params TEXT,created_at REAL);
    """)
    connection.execute("INSERT INTO images VALUES (?,?,?,?)", (image_id, "approved", 1, "train"))
    connection.execute("INSERT INTO source_validation_groups VALUES (?,?,?,?)",
                       ("test-group", "comma-source-conversion-v1:test", "train:day:lanes-1", json.dumps([image_id])))
    connection.execute("INSERT INTO source_conversion_checks VALUES (?,?,?,?,?,?)",
                       (image_id, "test-group", 1, sha256(source_path), str(geometry_path), sha256(geometry_path)))
    connection.executemany("INSERT INTO annotations VALUES (?,?,?,?,?,?)", [
        (1, image_id, "source_label", str(source_path), json.dumps({"format": "class_map", "human": True}), 1.),
        (2, image_id, "human", str(human_path), "{}", 2.),
    ])
    connection.commit()
    connection.close()
    return database, source_path, geometry_path, human_path


def test_evaluation_keeps_database_and_actual_annotations_byte_identical(tmp_path):
    paths = fixture_database(tmp_path)
    before = {str(path): path.read_bytes() for path in paths}
    report = evaluate(tmp_path, tmp_path / "development-output")
    assert report["totals"]["all"]["v3"]["f1"] == 1
    assert report["safety_checks"]["risk_selected_images"] == 0
    assert report["safety_checks"]["database_writes"] is False
    assert report["records"][0]["decision"] == "approved"
    assert all(Path(path).read_bytes() == value for path, value in before.items())


def test_evaluation_refuses_modified_immutable_conversion(tmp_path):
    _, _, geometry_path, _ = fixture_database(tmp_path)
    geometry_path.write_text(geometry_path.read_text() + " ")
    with pytest.raises(ValueError, match="fingerprint changed"):
        evaluate(tmp_path, tmp_path / "development-output")
