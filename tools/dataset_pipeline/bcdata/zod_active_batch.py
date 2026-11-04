"""Select only a fully staged ZOD annotation version."""
import json


def active_batch(root):
    for name in ("repairs/boundary-consolidation-v6", "repairs/continuous-boundaries-v5", "repairs/continuous-boundaries-v4", "repairs/continuous-boundaries-v3", "repairs/observed-curves-v2"):
        path = root / name / "repair_status.json"
        if path.exists() and json.loads(path.read_text()).get("state") == "audit_ready":
            return root / name
    return root
