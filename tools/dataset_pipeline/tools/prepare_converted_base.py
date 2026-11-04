"""Freeze the latest reviewed converted labels and reserve all assigned splits."""
import datetime
import json
from pathlib import Path
import sqlite3
import sys

PIPE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPE))

from bcdata.bootstrap import atomic_json
from bcdata.local import local_settings
from bcdata.review_export import export_reviewed
from bcdata.source_validation import refresh_groups
from bcdata.ufld_baseline import freeze_export


def main():
    settings = local_settings()
    plan = json.loads(settings.path("reports", "converted_training_plan.json").read_text())
    output = Path(plan["base_run"]).resolve()
    if not output.is_relative_to(settings.path("models", "ufld_baseline").resolve()):
        raise ValueError("Converted base must stay under the local baseline directory")
    report = settings.path("reports", "converted_training_preparation.json")
    state = json.loads(report.read_text()) if report.exists() else {}

    def update(phase, **values):
        state.update(phase=phase, updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), **values)
        atomic_json(report, state)
        print(json.dumps(state), flush=True)

    try:
        update("refreshing_reviewed_export", base_run=str(output))
        refresh_groups(settings)
        ready = export_reviewed(settings)
        if ready.get("errors") or not ready.get("validation", {}).get("ok"):
            raise ValueError("Latest converted export failed validation")
        update("freezing_latest_cleared_base", export_revision=ready["revision"])
        output.mkdir(exist_ok=False)
        snapshot, records, marker = freeze_export(settings, output)
        # Include reserved and currently ineligible identities, sessions, and
        # content hashes so additions cannot introduce held-out leakage.
        with sqlite3.connect(str(settings.db_path)) as con:
            con.row_factory = sqlite3.Row
            inventory = [dict(row) for row in con.execute(
                "SELECT image_id, source, session_id, split, sha256 FROM images WHERE split IS NOT NULL ORDER BY image_id")]
        atomic_json(snapshot / "split_inventory.json", inventory)
        update("cleared_base_ready", base_run=str(output), records=len(records),
               train_images=sum(row["split"] == "train" for row in records),
               val_images=sum(row["split"] == "val" for row in records),
               reserved_test_images=len(json.loads((snapshot / "reserved_test_ids.json").read_text())),
               export_revision=marker["revision"], split_inventory_records=len(inventory))
    except BaseException as error:
        update("error", error_type=type(error).__name__, error=str(error)[:1000])
        raise


if __name__ == "__main__":
    main()
