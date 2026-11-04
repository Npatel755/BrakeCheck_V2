"""Portable configuration for the imported dataset and local human review."""
from pathlib import Path
import os

from .config import REPO_ROOT, ensure_layout, load_settings

PROJECT_ROOT = REPO_ROOT.parent.parent


def local_settings(data_root=None):
    root = Path(data_root or os.environ.get("BRAKECHECK_DATA_ROOT", PROJECT_ROOT / "BrakeCheck_Dataset")).resolve()
    settings = load_settings(overrides={
        "data_root": str(root),
        "teachers": {"order": ["source_labels", "comma_segnet"]},
        "export": {"reviewed_only": True, "ufld": {"backbone": "34"}},
    })
    ensure_layout(settings)
    return settings
