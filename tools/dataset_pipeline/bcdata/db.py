"""SQLite metadata registry. One row per image, plus provenance for every annotation and review.

Rules enforced here rather than in each stage:
* an image whose review is final (`locked=1`) never gets its human annotation replaced by a model;
* annotation rows are unique per (image, kind, producer, model_version), so re-running a stage is idempotent.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_conversion_checks (
    image_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    state TEXT NOT NULL,
    geometry_id INTEGER NOT NULL,
    geometry_version TEXT NOT NULL,
    geometry_path TEXT NOT NULL,
    geometry_sha256 TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    diagnostics TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_validation_groups (
    group_key TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    stratum TEXT NOT NULL,
    sample_ids TEXT NOT NULL,
    state TEXT NOT NULL,
    evidence TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    frame_index INTEGER,
    path TEXT NOT NULL,
    width INTEGER, height INTEGER,
    sha256 TEXT NOT NULL,
    phash TEXT,
    license_id TEXT NOT NULL,
    pool TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ingested',
    reject_reason TEXT,
    quality TEXT,
    tags TEXT,
    cluster INTEGER,
    split TEXT,
    review_status TEXT NOT NULL DEFAULT 'none',
    locked INTEGER NOT NULL DEFAULT 0,
    has_source_label INTEGER NOT NULL DEFAULT 0,
    created_at REAL, updated_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS images_sha ON images(sha256);
CREATE INDEX IF NOT EXISTS images_status ON images(status);
CREATE INDEX IF NOT EXISTS images_session ON images(session_id);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL REFERENCES images(image_id),
    kind TEXT NOT NULL,            -- source_label | teacher | fused | geometry | human
    producer TEXT NOT NULL,        -- teacher name, 'fusion', 'geometry', reviewer name, dataset name
    model_version TEXT NOT NULL,   -- checkpoint id / config hash / 'human'
    checkpoint_sha TEXT,
    license_ids TEXT,              -- json list: every licence this output depends on
    path TEXT NOT NULL,
    params TEXT,
    created_at REAL,
    UNIQUE(image_id, kind, producer, model_version)
);
CREATE INDEX IF NOT EXISTS ann_image ON annotations(image_id);
CREATE INDEX IF NOT EXISTS ann_latest_kind ON annotations(image_id,kind,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS ann_latest_producer ON annotations(image_id,kind,producer,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS source_check_group ON source_conversion_checks(group_key,state);

CREATE TABLE IF NOT EXISTS qc (
    image_id TEXT PRIMARY KEY REFERENCES images(image_id),
    score REAL, tier TEXT, priority REAL,
    issues TEXT, qc_sample INTEGER DEFAULT 0,
    geometry_version TEXT, updated_at REAL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL REFERENCES images(image_id),
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,        -- approved | corrected | rejected | difficult
    seconds REAL,
    batch TEXT,
    changed_lanes INTEGER,
    mean_shift_px REAL,
    notes TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS models (
    model_version TEXT PRIMARY KEY,
    teacher TEXT NOT NULL,
    checkpoint_path TEXT,
    sha256 TEXT,
    license_ids TEXT,
    parent TEXT,
    metrics TEXT,
    promoted INTEGER DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT, params TEXT, counts TEXT,
    started REAL, finished REAL, ok INTEGER
);

CREATE TABLE IF NOT EXISTS downloads (
    url TEXT PRIMARY KEY, dest TEXT, bytes INTEGER, sha256 TEXT, completed_at REAL
);
"""


def _now() -> float:
    return time.time()


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- images -------------------------------------------------------------------------------
    def image_by_sha(self, sha: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM images WHERE sha256=?", (sha,)).fetchone()

    def image(self, image_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()

    def add_image(self, **row: Any) -> bool:
        """Insert an image. Returns False when the same bytes are already registered (exact duplicate)."""
        if self.image_by_sha(row["sha256"]) is not None:
            return False
        row.setdefault("created_at", _now())
        row["updated_at"] = _now()
        for key in ("quality", "tags"):
            if isinstance(row.get(key), (dict, list)):
                row[key] = json.dumps(row[key])
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT INTO images ({cols}) VALUES ({marks})", tuple(row.values()))
        return True

    def update_image(self, image_id: str, **fields: Any) -> None:
        for key in ("quality", "tags"):
            if isinstance(fields.get(key), (dict, list)):
                fields[key] = json.dumps(fields[key])
        fields["updated_at"] = _now()
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE images SET {sets} WHERE image_id=?", (*fields.values(), image_id))

    def images(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM images WHERE {where} ORDER BY image_id", tuple(params)).fetchall()

    def tags(self, row: sqlite3.Row) -> dict:
        return json.loads(row["tags"]) if row["tags"] else {}

    def merge_tags(self, image_id: str, new: dict, origin: str) -> dict:
        row = self.image(image_id)
        tags = self.tags(row) if row else {}
        for name, value in new.items():
            # human tags win; never overwrite them with model or heuristic guesses
            if tags.get(name, {}).get("origin") == "human" and origin != "human":
                continue
            tags[name] = {"value": value, "origin": origin}
        self.update_image(image_id, tags=tags)
        return tags

    # ---- annotations --------------------------------------------------------------------------
    def add_annotation(self, image_id: str, kind: str, producer: str, model_version: str, path: str,
                       license_ids: list[str] | None = None, checkpoint_sha: str | None = None,
                       params: dict | None = None) -> None:
        row = self.image(image_id)
        if row is None:
            raise KeyError(image_id)
        candidate_only = kind == "teacher" and (params or {}).get("candidate_only") is True
        if kind in ("teacher", "fused", "geometry") and row["locked"] and self._has_human(image_id) and not candidate_only:
            # Human-approved labels are final; unflagged model output stays out of the registry.
            # Explicit comparison-only teacher records cannot replace human,
            # fused, or active automatic geometry, so they may be added later.
            return
        with self.tx() as c:
            c.execute(
                """INSERT INTO annotations (image_id, kind, producer, model_version, checkpoint_sha, license_ids, path, params, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(image_id, kind, producer, model_version) DO UPDATE SET
                     path=excluded.path, params=excluded.params, checkpoint_sha=excluded.checkpoint_sha,
                     license_ids=excluded.license_ids, created_at=excluded.created_at""",
                (image_id, kind, producer, model_version, checkpoint_sha, json.dumps(license_ids or []),
                 path, json.dumps(params or {}), _now()))

    def _has_human(self, image_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM annotations WHERE image_id=? AND kind='human'", (image_id,)).fetchone() is not None

    def annotation(self, image_id: str, kind: str, producer: str | None = None,
                   model_version: str | None = None) -> sqlite3.Row | None:
        q = "SELECT * FROM annotations WHERE image_id=? AND kind=?"
        args: list[Any] = [image_id, kind]
        if producer:
            q += " AND producer=?"
            args.append(producer)
        if model_version:
            q += " AND model_version=?"
            args.append(model_version)
        q += " ORDER BY created_at DESC LIMIT 1"
        return self.conn.execute(q, args).fetchone()

    def annotations(self, image_id: str, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute("SELECT * FROM annotations WHERE image_id=? AND kind=? ORDER BY created_at", (image_id, kind)).fetchall()
        return self.conn.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY created_at", (image_id,)).fetchall()

    def has_annotation(self, image_id: str, kind: str, producer: str, model_version: str) -> bool:
        row = self.annotation(image_id, kind, producer, model_version)
        return row is not None and Path(row["path"]).exists()

    # ---- qc / reviews / models / runs ---------------------------------------------------------
    def set_qc(self, image_id: str, score: float, tier: str, priority: float, issues: list[dict],
               qc_sample: bool, geometry_version: str) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO qc (image_id, score, tier, priority, issues, qc_sample, geometry_version, updated_at)
                         VALUES (?,?,?,?,?,?,?,?)
                         ON CONFLICT(image_id) DO UPDATE SET score=excluded.score, tier=excluded.tier,
                           priority=excluded.priority, issues=excluded.issues, qc_sample=excluded.qc_sample,
                           geometry_version=excluded.geometry_version, updated_at=excluded.updated_at""",
                      (image_id, score, tier, priority, json.dumps(issues), int(qc_sample), geometry_version, _now()))

    def add_review(self, image_id: str, reviewer: str, decision: str, seconds: float | None = None,
                   batch: str | None = None, changed_lanes: int | None = None, mean_shift_px: float | None = None,
                   notes: str | None = None) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO reviews (image_id, reviewer, decision, seconds, batch, changed_lanes, mean_shift_px, notes, created_at)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (image_id, reviewer, decision, seconds, batch, changed_lanes, mean_shift_px, notes, _now()))
            final = decision in ("approved", "corrected", "rejected")
            c.execute("UPDATE images SET review_status=?, locked=?, updated_at=? WHERE image_id=?",
                      (decision, 1 if final else 0, _now(), image_id))

    def register_model(self, model_version: str, teacher: str, checkpoint_path: str | None, sha256: str | None,
                       license_ids: list[str], parent: str | None = None, metrics: dict | None = None) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO models (model_version, teacher, checkpoint_path, sha256, license_ids, parent, metrics, created_at)
                         VALUES (?,?,?,?,?,?,?,?)
                         ON CONFLICT(model_version) DO UPDATE SET metrics=COALESCE(excluded.metrics, models.metrics)""",
                      (model_version, teacher, checkpoint_path, sha256, json.dumps(license_ids), parent,
                       json.dumps(metrics) if metrics else None, _now()))

    def promote_model(self, model_version: str, teacher: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE models SET promoted=0 WHERE teacher=?", (teacher,))
            c.execute("UPDATE models SET promoted=1 WHERE model_version=?", (model_version,))

    def promoted_model(self, teacher: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM models WHERE teacher=? AND promoted=1", (teacher,)).fetchone()

    @contextmanager
    def run(self, stage: str, params: dict | None = None) -> Iterator[dict]:
        counts: dict = {}
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs (stage, params, started, ok) VALUES (?,?,?,0)",
                            (stage, json.dumps(params or {}, default=str), _now()))
            run_id = cur.lastrowid
        ok = 0
        try:
            yield counts
            ok = 1
        finally:
            with self.tx() as c:
                c.execute("UPDATE runs SET counts=?, finished=?, ok=? WHERE id=?",
                          (json.dumps(counts, default=str), _now(), ok, run_id))

    def count(self, sql: str, params: Iterable[Any] = ()) -> int:
        return int(self.conn.execute(sql, tuple(params)).fetchone()[0])
