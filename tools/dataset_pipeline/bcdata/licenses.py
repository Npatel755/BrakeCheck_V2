"""Licence registry: decides which pool a source or model output lands in, and blocks what isn't allowed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

USABLE = {"cleared", "cleared_conditions", "share_alike"}


class LicenseError(RuntimeError):
    pass


@dataclass
class LicenseEntry:
    key: str
    group: str
    status: str
    pool: str
    data: dict

    @property
    def commercial(self) -> bool:
        return self.status in ("cleared", "cleared_conditions") and self.pool == "commercial"


class LicenseRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.entries: dict[str, LicenseEntry] = {}
        for group in ("datasets", "models", "software", "derived"):
            for key, data in (raw.get(group) or {}).items():
                status = data.get("status", "unverified")
                pool = data.get("pool") or ("commercial" if status in ("cleared", "cleared_conditions") else
                                            "share_alike" if status == "share_alike" else "quarantine")
                self.entries[key] = LicenseEntry(key, group, status, pool, data)

    def get(self, key: str) -> LicenseEntry:
        if key not in self.entries:
            # Unknown = unverified: allowed to run, but its output is quarantined.
            return LicenseEntry(key, "unknown", "unverified", "quarantine", {})
        return self.entries[key]

    def pool_for_source(self, key: str) -> str:
        entry = self.get(key)
        if entry.status == "blocked":
            raise LicenseError(f"{key}: licence status is 'blocked'; it can't enter any pool")
        if entry.status not in USABLE:
            return "quarantine"
        return entry.pool

    def check_model(self, key: str, allow_unverified: bool = False) -> LicenseEntry:
        entry = self.get(key)
        if entry.status == "blocked":
            raise LicenseError(f"model {key} is blocked in {self.path.name}")
        if entry.status not in USABLE and not allow_unverified:
            raise LicenseError(f"model {key} is '{entry.status}' in {self.path.name}; clear it or pass --allow-unverified "
                               f"(its outputs then go to the quarantine pool)")
        return entry

    def output_pool(self, image_pool: str, model_keys: list[str]) -> str:
        """Pool for an annotation: the most restrictive of the image's pool and every model used."""
        rank = {"commercial": 0, "share_alike": 1, "quarantine": 2}
        worst = image_pool
        for key in model_keys:
            entry = self.get(key)
            pool = "quarantine" if entry.status not in USABLE else ("share_alike" if entry.status == "share_alike" else "commercial")
            if rank[pool] > rank[worst]:
                worst = pool
        return worst

    def table(self) -> list[dict]:
        return [{"id": e.key, "group": e.group, "license": e.data.get("license", ""), "status": e.status,
                 "pool": e.pool if e.group == "datasets" else "", "obligations": "; ".join(e.data.get("obligations", []) or [])}
                for e in self.entries.values()]
