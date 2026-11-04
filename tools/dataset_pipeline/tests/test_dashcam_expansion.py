from bcdata.expand_dashcam import select_unseen, split_for


def test_streaming_import_preserves_old_reviews_and_publishes_prepared_chunks(tmp_path, monkeypatch):
    import json
    import sys
    import uuid
    import numpy as np
    from PIL import Image
    from bcdata import expand_dashcam
    from bcdata.review_store import ReviewStore
    from tests.test_review import build_review_fixture
    settings = build_review_fixture(tmp_path / "data")
    store = ReviewStore(settings)
    store.decide("sample-0", "approved", "Nick", 0, str(uuid.uuid4()))
    before = store.detail("sample-0")
    folder = settings.path("sources", "fixture-stream")
    folder.mkdir(parents=True)
    items = [{"img": f"imgs/new-{i}.png", "mask": f"masks/new-{i}.png", "img_meta": {"size": 100, "sha": "fixture"},
              "mask_meta": {"size": 100, "sha": "fixture"}, "session": f"new-recording-{i}", "frame": 0, "name": f"new-{i}.png"} for i in range(2)]
    (folder / "plan.json").write_text(json.dumps({"repo": "fixture", "revision": "fixture", "items": items,
                                               "existing_ids": [f"sample-{i}" for i in range(3)]}))
    def fake_download(url, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.name == "LICENSE.txt":
            dest.write_text("Permission is hereby granted")
        elif "masks" in str(dest):
            array = np.full((874, 1164, 3), [64, 32, 32], np.uint8)
            array[400:700, 500:510] = [255, 0, 0]
            Image.fromarray(array).save(dest)
        else:
            array = np.asarray(Image.open(settings.path("raw", "sample-0.png"))).copy()
            array[:, :, 1] += 5
            Image.fromarray(array).save(dest)
        return dest
    monkeypatch.setattr(expand_dashcam, "download", fake_download)
    monkeypatch.setattr(expand_dashcam, "local_settings", lambda _: settings)
    monkeypatch.setattr(sys, "argv", ["expand", "--batch", "fixture-stream", "--limit", "2", "--chunk-size", "1"])
    expand_dashcam.main()
    result = json.loads(settings.path("reports", "dashcam_expansion.json").read_text())
    assert result["state"] == "ready" and result["count"] == 1
    assert store.detail("sample-0")["geometry"] == before["geometry"]
    assert store.detail("sample-0")["review_status"] == "approved"
    detail = store.detail(result["ready"][0])
    assert detail["review_status"] not in ("approved", "corrected", "rejected")
    assert detail["split"] in ("train", "val")
    assert detail["geometry"] is not None


def test_selection_excludes_existing_recordings_duplicate_sessions_and_interior():
    items = [
        {"session": "held-out", "img": "imgs/existing.png"},
        {"session": "new", "img": "imgsd/interior.png"},
        {"session": "new", "img": "imgs/road.png"},
        {"session": "new", "img": "imgs/adjacent.png"},
        {"session": "other", "img": "imgs/other.png"},
    ]
    selected = select_unseen(items, {"held-out"}, 2)
    assert [item["img"] for item in selected] == ["imgs/road.png", "imgs/other.png"]
    assert {split_for(item["session"]) for item in selected} <= {"train", "val"}


def test_fresh_candidate_cache_recovers_missing_maps_without_touching_human_labels(tmp_path):
    import json
    from pathlib import Path
    import uuid
    import numpy as np
    from bcdata.paint_proposals import generate_candidate, NAME
    from bcdata.review_store import ReviewStore
    from bcdata.teachers.base import TeacherOutput
    from tests.test_review import build_review_fixture
    settings = build_review_fixture(tmp_path / "data")
    store = ReviewStore(settings)
    store.decide("sample-0", "approved", "Nick", 0, str(uuid.uuid4()))
    original = store.detail("sample-0")["geometry"]
    class Teacher:
        calls = 0
        def predict(self, image, row):
            self.calls += 1
            return TeacherOutput(np.zeros(image.shape[:2], np.float32), np.zeros(image.shape[:2], np.float32))
    teacher = Teacher()
    with store.registry() as reg:
        row = reg.image("sample-0")
        assert generate_candidate(settings, reg, teacher, row) is True
        assert generate_candidate(settings, reg, teacher, row) is False
        ann = reg.annotation("sample-0", "teacher", NAME)
        Path(json.loads(Path(ann["path"]).read_text())["maps"]["lane"]).unlink()
        assert generate_candidate(settings, reg, teacher, row) is True
    assert teacher.calls == 2
    assert store.detail("sample-0")["geometry"] == original
