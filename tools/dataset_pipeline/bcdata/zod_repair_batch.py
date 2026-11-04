"""Stage a complete versioned ZOD repair without overwriting any human history."""
from concurrent.futures import ProcessPoolExecutor
import argparse
import json
from pathlib import Path
import sqlite3
import time
from itertools import islice

from filelock import FileLock

from .bootstrap import atomic_json
from .zod_audit_groups import ensure_plan
from .zod_mirror import CONVERTER, connect, convert, encoded, sha

REPAIR = "repairs/observed-curves-v2"


def convert_row(row):
    image_id, frame_id, native_json, native_sha, image_path, image_sha, ready = row
    if sha(native_json.encode()) != native_sha:
        raise ValueError("Native annotation changed: " + image_id)
    geometry, warnings = convert(json.loads(native_json), image_id)
    raw = encoded(geometry)
    return (image_id, frame_id, native_json, native_sha, raw, sha(raw.encode()),
            encoded(warnings), image_path, image_sha, ready)


def run(data_root, workers=4):
    root = Path(data_root).resolve() / "experiments/zod_mirror"
    target = root / REPAIR
    target.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target / ".repair.lock"), timeout=0):
        source = sqlite3.connect((root / "audits.sqlite").as_uri() + "?mode=ro", uri=True)
        db = connect(target)
        # Resume only the identical converter recipe.
        files = [Path(__file__), Path(__file__).with_name("zod_converter_v2.py"),
                 Path(__file__).with_name("zod_mirror.py"), Path(__file__).with_name("geometry.py")]
        recipe = {path.name: sha(path.read_bytes()) for path in files}
        receipt = target / "recipe.json"
        if receipt.exists() and json.loads(receipt.read_text()) != recipe:
            raise ValueError("The staged converter changed; use a new repair version")
        atomic_json(receipt, recipe)
        total = source.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        existing = {row[0] for row in db.execute("SELECT image_id FROM frames")}
        status = {"state": "converting", "version": CONVERTER, "total": total,
                  "done": len(existing), "started": time.time(), "human_decisions_created": 0,
                  "original_database_unchanged": True, "training_eligible": False}
        atomic_json(target / "repair_status.json", status)
        rows = (tuple(row) for row in source.execute("SELECT image_id,frame_id,native_json,native_sha,image_path,image_sha,image_ready FROM frames ORDER BY image_id") if row[0] not in existing)
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                while batch := list(islice(rows, 1000)):
                    for row in pool.map(convert_row, batch, chunksize=16):
                        db.execute("INSERT INTO frames(image_id,frame_id,native_json,native_sha,geometry_json,geometry_sha,qc_json,image_path,image_sha,image_ready) VALUES(?,?,?,?,?,?,?,?,?,?)", row)
                        status["done"] += 1
                    if status["done"]:
                        db.commit()
                        atomic_json(target / "repair_status.json", status)
            db.commit()
            if any(sha(path.read_bytes()) != recipe[path.name] for path in files):
                raise ValueError("Converter code changed while preparing the repair")
            status["state"] = "building_sample_plan"
            atomic_json(target / "repair_status.json", status)
            ensure_plan(db)
            status.update(state="audit_ready", completed=time.time(),
                          groups=db.execute("SELECT COUNT(*) FROM zod_audit_groups").fetchone()[0],
                          samples=db.execute("SELECT COUNT(*) FROM zod_audit_members WHERE is_sample=1").fetchone()[0])
            atomic_json(target / "repair_status.json", status)
        except Exception as error:
            db.rollback()
            status.update(state="error", error=str(error))
            atomic_json(target / "repair_status.json", status)
            raise
        finally:
            db.close()
            source.close()
        return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(run(args.data_root, args.workers)))
