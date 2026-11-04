"""Import targeted inspection tasks without replacing any saved lane labels."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import json
import sys

PIPELINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE))
from bcdata.local import local_settings
from bcdata.lanes import current_geometry
from bcdata.review_store import ReviewStore


def saved_state(store, image_ids):
    result = {}
    with store.registry() as reg:
        for image_id in image_ids:
            row = reg.image(image_id)
            if row is None:
                raise ValueError(f"Unknown image: {image_id}")
            geometry = current_geometry(reg, image_id)
            result[image_id] = {
                "review_status": row["review_status"], "locked": row["locked"],
                "split": row["split"], "revision": store._revision(reg, image_id),
                "geometry_sha256": hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest(),
                "annotations": [tuple(r) for r in reg.conn.execute(
                    "SELECT * FROM annotations WHERE image_id=? ORDER BY id", (image_id,))],
                "review_events": [tuple(r) for r in reg.conn.execute(
                    "SELECT * FROM review_events WHERE image_id=? ORDER BY created_at,event_id", (image_id,))],
            }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--data-root")
    parser.add_argument("--batch-id", default="ufld-missed-lanes-20261020")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    candidates = report["candidates"]
    image_ids = [candidate["image_id"] for candidate in candidates]
    if len(set(image_ids)) != len(image_ids):
        raise ValueError("Duplicate image IDs in audit report")
    store = ReviewStore(local_settings(args.data_root))
    before = saved_state(store, image_ids)
    if args.dry_run:
        print(json.dumps({"mode": "dry_run", "count": len(image_ids), "image_ids": image_ids}, indent=2))
        return
    result = store.enqueue_label_checks(candidates, args.batch_id)
    after = saved_state(store, image_ids)
    if after != before:
        raise RuntimeError("Saved annotation state changed during import; inspect concurrent review activity")
    with store.registry() as reg:
        tasks = [dict(row) for row in reg.conn.execute(
            "SELECT image_id,batch_id,state FROM label_review_checks WHERE image_id IN ("
            + ",".join("?" for _ in image_ids) + ") ORDER BY image_id", image_ids)] if image_ids else []
    receipt = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_filter": "label_check", "queue_name": "UFLD label checks", **result,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "saved_annotations_unchanged": before == after, "tasks": tasks,
        "prior_state": {image_id: {k: value for k, value in state.items()
                                  if k not in ("annotations", "review_events")}
                        for image_id, state in before.items()},
    }
    receipt_path = args.report.parent / "label_check_queue_receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({**result, "saved_annotations_unchanged": True,
                      "receipt": str(receipt_path)}, indent=2))


if __name__ == "__main__":
    main()
