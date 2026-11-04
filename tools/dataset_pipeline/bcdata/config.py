"""Config loading and the dataset folder layout."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "pipeline.yaml"
DEFAULT_LICENSES = REPO_ROOT / "config" / "licenses.yaml"

# The folder tree from the task spec. Created by `bcdata init`.
LAYOUT = [
    "config", "sources",
    "raw/comma10k", "raw/comma2k19", "raw/carla", "raw/zod",
    "extracted_frames", "curated_images",
    "annotations/teacher_predictions", "annotations/fused_predictions", "annotations/reviewed",
    "lane_geometry", "road_masks", "review_queue",
    "exports/ufld_v2", "exports/semantic_segmentation",
    "reports", "models", "logs", "tests",
]


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Settings:
    cfg: dict
    config_path: Path
    licenses_path: Path

    @property
    def root(self) -> Path:
        root = Path(self.cfg["data_root"])
        return root if root.is_absolute() else (REPO_ROOT / root)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def db_path(self) -> Path:
        return self.path("metadata.sqlite")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.cfg
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def load_settings(config_path: str | Path | None = None, overrides: dict | None = None,
                  licenses_path: str | Path | None = None) -> Settings:
    base = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if config_path:
        base = _deep_merge(base, yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {})
    if overrides:
        base = _deep_merge(base, overrides)
    return Settings(base, path, Path(licenses_path) if licenses_path else DEFAULT_LICENSES)


def ensure_layout(settings: Settings) -> list[Path]:
    made = []
    for rel in LAYOUT:
        p = settings.path(rel)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            made.append(p)
    return made


def setup_logging(settings: Settings | None = None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings is not None:
        log_dir = settings.path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True)
