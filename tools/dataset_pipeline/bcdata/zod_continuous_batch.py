"""Build fresh continuous-path ZOD audits from the frozen observed conversion."""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
import json
from pathlib import Path
import sqlite3
import time

from filelock import FileLock

from .bootstrap import atomic_json
from .lane_boundary_completion import FULL_PATH_POLICY as POLICY, complete_lane_paths as complete_boundaries
from .zod_audit_groups import ensure_plan
from .zod_mirror import connect, encoded, sha
from .zod_repair_batch import REPAIR as PREVIOUS

REPAIR = "repairs/continuous-boundaries-v5"
VERSION = "zod-continuous-boundaries-v5"


def transform_row(row):
    image_id, frame_id, native_json, native_sha, geometry_json, geometry_sha, qc_json, image_path, image_sha, ready = row
    if sha(geometry_json.encode()) != geometry_sha:
        raise ValueError("Observed conversion changed")
    geometry = complete_boundaries(json.loads(geometry_json))
    geometry.update(version=VERSION, parent_geometry_sha256=geometry_sha)
    raw = encoded(geometry)
    completion = geometry["boundary_completion"]
    stats = {"interpolated_gaps": completion["interpolated_gaps"],
             "photos_connected": int(completion["interpolated_gaps"] > 0),
             "blocked_gaps": len(completion["blocked_gaps"])}
    stats['association_issues'] = int(completion['requires_repair'])
    return (image_id, frame_id, native_json, native_sha, raw, sha(raw.encode()), qc_json, image_path, image_sha, ready), stats


def run(data_root):
    root = Path(data_root).resolve() / "experiments/zod_mirror"
    target = root / REPAIR
    target.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target / ".repair.lock"), timeout=0):
        published = target / 'repair_status.json'
        if published.exists():
            existing = json.loads(published.read_text())
            if existing.get('state') == 'audit_ready':
                # A published batch now contains live human reviews. A repeat
                # launch must never replay older decisions over those reviews.
                return existing
        previous = root / PREVIOUS
        if json.loads((previous / "repair_status.json").read_text())["state"] != "audit_ready":
            raise ValueError("Complete observed source conversion is required")
        source = sqlite3.connect((previous / "audits.sqlite").as_uri() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        db = connect(target)
        files = [Path(__file__), Path(__file__).with_name("lane_boundary_completion.py"), Path(__file__).with_name('zod_review_migration.py')]
        recipe = {path.name: sha(path.read_bytes()) for path in files}
        receipt = target / "recipe.json"
        if receipt.exists() and json.loads(receipt.read_text()) != recipe:
            raise ValueError("Continuous conversion recipe changed; use a new version")
        atomic_json(receipt, recipe)
        done = {r[0] for r in db.execute("SELECT image_id FROM frames")}
        prior_status = json.loads((target / "repair_status.json").read_text()) if (target / "repair_status.json").exists() else {}
        stats = Counter({key: prior_status.get(key, 0) for key in ("interpolated_gaps", "photos_connected", "blocked_gaps", 'association_issues')})
        status = {"state": "converting", "version": VERSION, "policy": POLICY,
                  "done": len(done), "total": source.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                  "human_decisions_created": 0, "prior_reviews_preserved": True,
                  "training_eligible": False, "started": time.time()}
        atomic_json(target / "repair_status.json", status)
        try:
            rows = (tuple(row) for row in source.execute("SELECT image_id,frame_id,native_json,native_sha,geometry_json,geometry_sha,qc_json,image_path,image_sha,image_ready FROM frames ORDER BY image_id") if row["image_id"] not in done)
            with ProcessPoolExecutor(max_workers=4) as pool:
                while batch := list(islice(rows, 1000)):
                    for row, row_stats in pool.map(transform_row, batch, chunksize=16):
                        db.execute("INSERT INTO frames(image_id,frame_id,native_json,native_sha,geometry_json,geometry_sha,qc_json,image_path,image_sha,image_ready) VALUES(?,?,?,?,?,?,?,?,?,?)", row)
                        stats.update(row_stats)
                        status["done"] += 1
                    db.commit()
                    status.update(stats)
                    atomic_json(target / "repair_status.json", status)
            db.commit()
            status["state"] = "building_sample_plan"
            atomic_json(target / "repair_status.json", status)
            # Keep the existing sample plan stable; user decisions do not
            # select new samples or invalidate independent model caches.
            ensure_plan(db)
            from .zod_review_migration import carry_reviews
            status.update(carry_reviews(root, db))
            for row in db.execute('SELECT image_id,geometry_json FROM frames'):
                issues = json.loads(row['geometry_json'])['boundary_completion']['association_issues']
                if issues:
                    db.execute('INSERT OR REPLACE INTO zod_conversion_findings VALUES(?,?,?)',
                               (row['image_id'], encoded(issues), VERSION))
                    db.execute('UPDATE zod_audit_members SET repair_required=1 WHERE image_id=?', (row['image_id'],))
            db.commit()
            db.execute("CREATE TABLE IF NOT EXISTS zod_model_proposals (image_id TEXT,model TEXT,version TEXT,geometry_json TEXT,geometry_sha TEXT,source_binding TEXT,PRIMARY KEY(image_id,model))")
            samples = {r[0] for r in db.execute("SELECT image_id FROM zod_audit_members WHERE is_sample=1")}
            # Reuse exact independent inference, changing only the path target.
            for row in source.execute("SELECT * FROM zod_model_proposals"):
                if sha(row["geometry_json"].encode()) != row["geometry_sha"]:
                    raise ValueError("Frozen model proposal changed")
                geometry = complete_boundaries(json.loads(row["geometry_json"]))
                version = row["version"] + ":continuous-v5"
                geometry.update(teacher_version=version, parent_proposal_sha256=row["geometry_sha"])
                raw = encoded(geometry)
                db.execute("INSERT OR IGNORE INTO zod_model_proposals VALUES(?,?,?,?,?,?)", (row["image_id"], row["model"], version, raw, sha(raw.encode()), row["source_binding"]))
            db.commit()
            counts = dict(db.execute("SELECT image_id,COUNT(*) FROM zod_model_proposals GROUP BY image_id"))
            if any(counts.get(image_id) != 2 for image_id in samples):
                raise ValueError("Each fresh audit sample needs both model proposals")
            if any(sha(path.read_bytes()) != recipe[path.name] for path in files):
                raise ValueError("Continuous conversion recipe changed during rebuild")
            status.update(state="audit_ready", completed=time.time(),
                          groups=db.execute("SELECT COUNT(*) FROM zod_audit_groups").fetchone()[0], samples=len(samples), **stats)
            atomic_json(target / "proposal_status.json", {"state": "complete", "samples": len(samples),
                        "total": 2 * len(samples), "done": 2 * len(samples),
                        "inference_reused": True, "training_performed": False})
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
    args = parser.parse_args()
    print(json.dumps(run(args.data_root)))
