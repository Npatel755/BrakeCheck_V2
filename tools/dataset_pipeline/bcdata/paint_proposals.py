"""Fresh CPU road-marking candidates; never replace current or human geometry."""
import argparse
import json
import logging
import time
from pathlib import Path

from .bootstrap import atomic_json
from .db import Registry
from .geometry import extract_lanes
from .lanes import params_for
from .local import local_settings
from .quality import read_rgb
from .road_geometry import road_edges_from_probability
from .teachers.base import save_output
from .teachers.comma_segnet import CommaSegnetTeacher

NAME = "comma_segnet_fresh"
REVISION = "b642c614eb0737fe7befa229f47fbd0245033038"
VERSION = NAME + ":" + REVISION + ":cpu-native-v1"


def generate_candidate(settings, reg, teacher, row):
    """Cache one independent candidate without changing active geometry or decisions."""
    path = settings.path("lane_geometry", "teachers", NAME, row["image_id"] + ".json")
    existing = reg.annotation(row["image_id"], "teacher", NAME, VERSION)
    if existing and Path(existing["path"]).is_file() and path.exists() and json.loads(path.read_text()).get("teacher_version") == VERSION:
        metadata = json.loads(Path(existing["path"]).read_text(encoding="utf-8"))
        if "lane" in metadata.get("maps", {}) and "road" in metadata["maps"] and all(Path(value).is_file() for value in metadata["maps"].values()):
            return False
    image = read_rgb(row["path"])
    if image is None:
        raise ValueError("Unreadable photo")
    output = teacher.predict(image, row)
    output.meta.update(source_guided=False, reference_inputs=[], model_forwards=1,
                       repository_revision=REVISION, license="MIT",
                       evaluation_caveat="Pretrained on comma10k; not an independent held-out benchmark")
    geometry = extract_lanes(output.lane, params_for(settings))
    geometry.update(image_id=row["image_id"], teacher=NAME, teacher_version=VERSION,
                    source_guided=False, model_meta=output.meta,
                    road_edges=road_edges_from_probability(output.road, occlusion_mask=output.extra.get("movable")))
    paths = save_output(settings, NAME, VERSION, row["image_id"], output)
    atomic_json(path, geometry)
    reg.add_annotation(row["image_id"], "teacher", NAME, VERSION, paths["_meta"],
                       license_ids=[row["license_id"], "comma_segnet"], checkpoint_sha=REVISION,
                       params={"candidate_only": True, "source_guided": False})
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=["train", "val", "test"])
    args = parser.parse_args()
    import torch
    import cv2
    from filelock import FileLock
    torch.set_num_threads(4)
    cv2.setNumThreads(1)
    settings = local_settings()
    settings.cfg["teachers"].update(device="cpu", amp=False)
    settings.cfg["teachers"]["comma_segnet"]["revision"] = REVISION
    report = settings.path("reports", "paint_proposals.json")
    status = {"state": "loading", "model": NAME, "version": VERSION, "device": "cpu", "done": 0, "failed": 0}
    atomic_json(report, status)
    with FileLock(str(settings.path("logs", "paint-proposals.lock")), timeout=0):
        reg = Registry(settings.db_path)
        teacher = CommaSegnetTeacher(settings)
        try:
            teacher.load()
            where, arguments = "status='curated'", []
            if args.split:
                where += " AND split=?"
                arguments.append(args.split)
            rows = reg.images(where, arguments)
            if args.limit:
                rows = rows[:args.limit]
            status.update(state="running", total=len(rows))
            for row in rows:
                try:
                    started = time.monotonic()
                    generate_candidate(settings, reg, teacher, row)
                    status.update(done=status["done"] + 1, last_image=row["image_id"],
                                  last_seconds=round(time.monotonic() - started, 3))
                except Exception as error:
                    status.update(failed=status["failed"] + 1, error_type=type(error).__name__)
                    logging.exception("CPU proposal failed")
                atomic_json(report, status)
            status["state"] = "complete" if not status["failed"] else "partial_error"
            atomic_json(report, status)
        finally:
            teacher.unload()
            reg.close()
    print(json.dumps(status))


if __name__ == "__main__":
    main()
