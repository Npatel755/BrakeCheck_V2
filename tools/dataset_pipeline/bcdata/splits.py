"""Leak-free splits: whole recording sessions go to one split, and an assignment never changes once made.

`test` doubles as the fixed evaluation set: it is excluded from teacher training, fine-tuning and UFLD
training. `val` is used for checkpoint selection. Synthetic sessions never enter `test`.
"""
from __future__ import annotations

import hashlib

from .config import Settings
from .db import Registry


def _bucket(session_id: str, seed: int) -> float:
    h = hashlib.sha256(f"{seed}:{session_id}".encode()).hexdigest()
    return int(h[:12], 16) / float(1 << 48)


def split_for(session_id: str, synthetic: bool, fractions: dict, seed: int) -> str:
    u = _bucket(session_id, seed)
    train, val = float(fractions.get("train", 0.8)), float(fractions.get("val", 0.1))
    if u < train:
        return "train"
    if u < train + val:
        return "val"
    return "val" if synthetic else "test"     # the evaluation set is real-world only


def assign(settings: Settings, registry: Registry) -> dict:
    fractions = settings.get("export.splits", {"train": 0.8, "val": 0.1, "test": 0.1})
    seed = int(settings.get("seed", 0))
    known: dict[str, str] = {}
    for r in registry.images("split IS NOT NULL"):
        known.setdefault(r["session_id"], r["split"])
    counts = {"train": 0, "val": 0, "test": 0, "kept": 0}
    with registry.tx() as c:
        for r in registry.images("split IS NULL AND status IN ('ingested','curated')"):
            s = known.get(r["session_id"]) or split_for(r["session_id"], bool(r["synthetic"]), fractions, seed)
            known[r["session_id"]] = s
            c.execute("UPDATE images SET split=? WHERE image_id=?", (s, r["image_id"]))
            counts[s] += 1
    counts["kept"] = registry.count("SELECT COUNT(*) FROM images WHERE split IS NOT NULL")
    return counts
