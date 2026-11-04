"""Explicit maintenance action: back up and reopen accepted zero-lane reviews."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bcdata.bootstrap import atomic_json
from bcdata.lanes import current_geometry
from bcdata.local import local_settings
from bcdata.review_store import ReviewStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = local_settings()
    store = ReviewStore(settings)
    with store.registry() as reg:
        matches = []
        for row in reg.images("status='curated' AND locked=1 AND review_status IN ('approved','corrected')"):
            cur = current_geometry(reg, row["image_id"])
            if cur and not any(lane.get("slot") in (1, 2, 3, 4) for lane in cur[1].get("lanes", [])):
                matches.append(row["image_id"])
    if not args.apply:
        print(json.dumps({"eligible": len(matches), "applied": False}))
        return
    backup = store.backup()
    reopened = store.requeue_empty_lanes("Nick")
    report = {"created_at": time.time(), "backup": str(backup), "reopened_count": len(reopened), "events": reopened}
    atomic_json(settings.path("reports", "requeued_empty_lanes.json"), report)
    print(json.dumps({"reopened": len(reopened), "backup": str(backup)}))


if __name__ == "__main__":
    main()
