"""Isolated optional reviews of copied teacher proposals; never opens the main DB."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .proposal_policy import proposal_approval_policy, preferred_proposal_bytes
from .proposal_training_policy import training_geometry_hold

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


class ExperimentDecision(BaseModel):
    copy_id: str = Field(min_length=1, max_length=160)
    action: str
    event_id: str = Field(min_length=8, max_length=160)
    revision: int = Field(ge=0)
    proposal_sha256: str = Field(min_length=64, max_length=64)


def _name(value: str) -> str:
    if not _NAME.fullmatch(value) or ".." in value:
        raise HTTPException(400, "Invalid experiment or copy identifier")
    return value


def _inside(root: Path, value: str | Path) -> Path:
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise HTTPException(400, "Experiment path leaves its isolated directory")
    return path


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "Experiment artifact is not ready") from None
    except (OSError, ValueError):
        raise HTTPException(409, "Experiment artifact is being prepared; retry shortly") from None


class ExperimentReview:
    """All decisions belong to run/review.sqlite, separate from original reviews."""
    def __init__(self, data_root: Path):
        self.base = (Path(data_root) / "experiments" / "flagged_teacher").resolve()
        self._cache = {}
        self._lock = threading.Lock()

    def run(self, run_id: str) -> Path:
        path = _inside(self.base, _name(run_id))
        if not path.is_dir() or not (path / "experiment.json").is_file():
            raise HTTPException(404, "Experiment not found")
        return path

    def runs(self):
        if not self.base.is_dir():
            return []
        result = []
        for path in sorted(self.base.iterdir(), reverse=True):
            if not path.is_dir() or not _NAME.fullmatch(path.name) or ".." in path.name:
                continue
            try:
                run = self.run(path.name)
                metadata = _json(_inside(run, "experiment.json"))
            except HTTPException:
                continue
            result.append({"run_id": path.name, "metadata": metadata})
        return result

    def copies(self, run: Path):
        path = _inside(run, "copies.json")
        try:
            stat = path.stat()
        except FileNotFoundError:
            raise HTTPException(404, "Experiment copies are not ready") from None
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(str(run))
            if cached and cached[0] == key:
                return cached[1]
        manifest = _json(path)
        if not isinstance(manifest, dict) or manifest.get("schema") != "flagged_model_copies:v1" or not isinstance(manifest.get("items"), list):
            raise HTTPException(409, "Unsupported experiment copy manifest")
        items = {}
        for item in manifest["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("copy_id"), str):
                raise HTTPException(409, "Incomplete experiment copy manifest")
            copy_id = _name(item["copy_id"])
            if copy_id in items:
                raise HTTPException(409, "Duplicate experiment copy identifier")
            items[copy_id] = item
        with self._lock:
            self._cache[str(run)] = (key, items)
        return items

    def proposal(self, run: Path, copy_id: str):
        try:
            raw = preferred_proposal_bytes(run, _name(copy_id))
        except FileNotFoundError:
            raise HTTPException(404, "Fresh model proposal is not ready") from None
        except (ValueError, OSError):
            raise HTTPException(409, "Model proposal provenance changed; reload after preparation") from None
        try:
            proposal = json.loads(raw)
        except ValueError:
            raise HTTPException(409, "Model proposal is being prepared") from None
        if not isinstance(proposal, dict) or not isinstance(proposal.get("geometry"), dict):
            raise HTTPException(409, "Model proposal has no geometry")
        return proposal, hashlib.sha256(raw).hexdigest()

    def decisions(self, run: Path):
        path = _inside(run, "review.sqlite")
        if not path.exists():
            return {}
        try:
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA query_only=ON")
                return {row["copy_id"]: dict(row) for row in con.execute("SELECT * FROM decisions")}
            finally:
                con.close()
        except sqlite3.OperationalError:
            raise HTTPException(409, "Experiment decisions are initializing; retry shortly") from None

    @staticmethod
    def _qc(proposal):
        quality = proposal.get("quality", {})
        return isinstance(quality, dict) and quality.get("eligible_for_experiment") is True

    @staticmethod
    def snapshot_frozen(metadata):
        return bool(metadata.get("training_dataset")) or metadata.get("phase") in (
            "building_training_snapshot", "training_ufld", "complete_awaiting_comparison_review")

    def progress_reports(self, run, metadata):
        reports = {}
        candidates = {"teacher_prepare": "teacher/prepare_status.json", "teacher_training": "teacher/status.json",
                      "ufld_training": "ufld_training.json", "proposal_repair": "proposal_repair.json"}
        ufld = metadata.get("ufld_run")
        if isinstance(ufld, str) and ufld:
            candidates["ufld_run"] = str(Path(ufld) / "status.json")
        for key, relative in candidates.items():
            try:
                path = _inside(run, relative)
                if path.is_file():
                    reports[key] = _json(path)
            except HTTPException:
                # Optional progress never grants access outside this experiment.
                continue
        return reports

    def status(self, run_id: str):
        run = self.run(run_id)
        metadata = _json(_inside(run, "experiment.json"))
        try:
            copies = self.copies(run)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            copies = {}
        decisions = self.decisions(run)
        proposal_dir = _inside(run, "proposals")
        ready = {p.stem for p in proposal_dir.glob("*.json")} if proposal_dir.is_dir() else set()
        counts = {"copies": len(copies), "proposals_ready": len(ready & copies.keys()),
                  "train_copies": sum(item.get("split") == "train" for item in copies.values()),
                  "approved": 0, "excluded": 0, "skipped": 0}
        for decision in decisions.values():
            key = {"approve": "approved", "exclude": "excluded", "skip": "skipped"}.get(decision["action"])
            if key and decision["copy_id"] in copies:
                counts[key] += 1
        counts["optional_checks_remaining"] = max(0, counts["proposals_ready"] - sum(counts[k] for k in ("approved", "excluded", "skipped")))
        return {"run_id": run_id, "experimental": True, "original_reviews_unchanged": True,
                "metadata": metadata, "counts": counts, "progress": self.progress_reports(run, metadata),
                "snapshot_frozen": self.snapshot_frozen(metadata),
                "decisions_apply_to": "future_rebuild_only" if self.snapshot_frozen(metadata) else "next_experiment_snapshot",
                "policy": "Experiments do not wait for individual approvals: QC-passing training copies are selected automatically. Optional human approval can accept advisory quality holds when painted geometry is valid. Empty, errored or malformed predictions stay out; exclude and skip hold copies out. Original data and approved collections stay unchanged."}

    def detail(self, run_id: str, copy_id: str):
        run = self.run(run_id)
        item = self.copies(run).get(_name(copy_id))
        if item is None:
            raise HTTPException(404, "Experiment copy not found")
        proposal, digest = self.proposal(run, copy_id)
        original_path = item.get("original_geometry_path")
        original = _json(_inside(run, original_path)) if original_path else {"lanes": []}
        if isinstance(original, dict) and isinstance(original.get("geometry"), dict):
            original = original["geometry"]
        if not isinstance(original, dict):
            raise HTTPException(409, "Frozen original geometry is invalid")
        decision = self.decisions(run).get(copy_id)
        approval = proposal_approval_policy(proposal, width=item.get("width"), height=item.get("height"))
        training_geometry = training_geometry_hold(proposal.get("geometry"), item.get("width"), item.get("height"))
        if approval["approval_allowed"] and set(training_geometry["reasons"]) - {"probable_same_boundary_multiple_slots"}:
            approval = {"approval_allowed": False, "overrides_quality": False,
                        "block_reason": "The proposal lacks valid observed painted lines; regenerate or correct it."}
        automatic_pass = self._qc(proposal) and training_geometry["eligible_for_training"]
        return {"run_id": run_id, "copy_id": copy_id, "original_image_id": item.get("original_image_id"),
                "split": item.get("split"), "source": item.get("source"), "session_id": item.get("session_id"),
                "width": item.get("width"), "height": item.get("height"),
                "image_url": f"/api/experiments/flagged/{run_id}/copies/{copy_id}/image",
                "original_geometry": original, "proposal": proposal, "proposal_sha256": digest,
                "quality_passes": automatic_pass, "decision": decision,
                "training_geometry": training_geometry,
                "approval_allowed": approval["approval_allowed"],
                "approval_block_reason": approval["block_reason"],
                "approval_overrides_quality": approval["approval_allowed"] and not automatic_pass,
                "revision": decision["revision"] if decision else 0,
                "experimental": True, "original_reviews_unchanged": True}

    def queue(self, run_id: str, split="train", status="pending", limit=1, after=""):
        if split not in ("all", "train", "val", "test") or status not in ("pending", "all", "approve", "exclude", "skip"):
            raise HTTPException(400, "Invalid experiment queue filter")
        run = self.run(run_id)
        try:
            copies = self.copies(run)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return {"run_id": run_id, "items": [], "split": split, "status": status,
                    "next_after": None, "preparing_copies": True}
        decisions = self.decisions(run)
        directory = _inside(run, "proposals")
        ready = {p.stem for p in directory.glob("*.json")} if directory.is_dir() else set()
        result = []
        ids = sorted(copies)
        if after:
            _name(after)
            ids = [key for key in ids if key > after]
        for copy_id in ids:
            item = copies[copy_id]
            if copy_id not in ready or split != "all" and item.get("split") != split:
                continue
            decision = decisions.get(copy_id)
            if status == "pending" and decision or status not in ("pending", "all") and (not decision or decision["action"] != status):
                continue
            result.append(self.detail(run_id, copy_id))
            if len(result) >= limit:
                break
        return {"run_id": run_id, "items": result, "split": split, "status": status,
                "next_after": result[-1]["copy_id"] if result else None}

    def decide(self, run_id: str, body: ExperimentDecision, reviewer: str):
        run = self.run(run_id)
        if body.action not in ("approve", "exclude", "skip"):
            raise HTTPException(400, "Choose approve, exclude, or skip")
        current = self.detail(run_id, body.copy_id)
        if current["proposal_sha256"] != body.proposal_sha256:
            raise HTTPException(409, "The model proposal changed; reload this copy")
        if body.action == "approve" and not current["approval_allowed"]:
            raise HTTPException(422, current["approval_block_reason"])
        path = _inside(run, "review.sqlite")
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS decisions(
                    copy_id TEXT PRIMARY KEY, action TEXT NOT NULL, reviewer TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE, proposal_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS decision_events(
                    event_id TEXT PRIMARY KEY, copy_id TEXT NOT NULL, action TEXT NOT NULL,
                    reviewer TEXT NOT NULL, proposal_sha256 TEXT NOT NULL,
                    previous_revision INTEGER NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL);
            """)
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT * FROM decision_events WHERE event_id=?", (body.event_id,)).fetchone()
            if previous:
                if (previous["copy_id"], previous["action"], previous["reviewer"], previous["proposal_sha256"], previous["previous_revision"]) != (body.copy_id, body.action, reviewer, body.proposal_sha256, body.revision):
                    raise HTTPException(409, "Decision identifier was already used for another action")
                con.rollback()
                return {"copy_id": body.copy_id, "action": body.action, "revision": previous["revision"],
                        "event_id": body.event_id, "idempotent": True, "original_reviews_unchanged": True}
            row = con.execute("SELECT * FROM decisions WHERE copy_id=?", (body.copy_id,)).fetchone()
            revision = row["revision"] if row else 0
            if body.revision != revision:
                raise HTTPException(409, "Another reviewer changed this copy; reload it")
            # Check the immutable proposal fingerprint again under the decision transaction.
            if self.proposal(run, body.copy_id)[1] != body.proposal_sha256:
                raise HTTPException(409, "The model proposal changed; reload this copy")
            now = time.time()
            con.execute("INSERT INTO decision_events VALUES(?,?,?,?,?,?,?,?)",
                        (body.event_id, body.copy_id, body.action, reviewer, body.proposal_sha256, revision, revision + 1, now))
            con.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?) ON CONFLICT(copy_id) DO UPDATE SET action=excluded.action,reviewer=excluded.reviewer,event_id=excluded.event_id,proposal_sha256=excluded.proposal_sha256,revision=excluded.revision,updated_at=excluded.updated_at",
                        (body.copy_id, body.action, reviewer, body.event_id, body.proposal_sha256, revision + 1, now))
            con.commit()
            metadata = _json(_inside(run, "experiment.json"))
            return {"copy_id": body.copy_id, "action": body.action, "revision": revision + 1,
                    "event_id": body.event_id, "idempotent": False, "original_reviews_unchanged": True,
                    "decisions_apply_to": "future_rebuild_only" if self.snapshot_frozen(metadata) else "next_experiment_snapshot"}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def create_experiment_router(data_root: Path, require_session: Callable) -> APIRouter:
    """The host must pass its existing authenticated-session dependency."""
    router = APIRouter(prefix="/api/experiments/flagged", tags=["isolated experiment"])
    store = ExperimentReview(data_root)

    def authenticated(session=Depends(require_session)):
        if not isinstance(session, dict) or not session.get("reviewer"):
            raise HTTPException(401, "Pair this phone before reviewing experiment copies")
        return session

    @router.get("")
    def runs(session=Depends(authenticated)):
        return {"experiments": store.runs()}

    @router.get("/latest")
    def latest(session=Depends(authenticated)):
        available = store.runs()
        if not available:
            raise HTTPException(404, "No flagged-image experiment has been prepared yet")
        return store.status(available[0]["run_id"])

    @router.get("/{run_id}/status")
    def status(run_id: str, session=Depends(authenticated)):
        return store.status(run_id)

    @router.get("/{run_id}/queue")
    def queue(run_id: str, split: str = "train", status: str = "pending", limit: int = Query(1, ge=1, le=12), after: str = "", session=Depends(authenticated)):
        return store.queue(run_id, split, status, limit, after)

    @router.get("/{run_id}/copies/{copy_id}")
    def detail(run_id: str, copy_id: str, session=Depends(authenticated)):
        return store.detail(run_id, copy_id)

    @router.get("/{run_id}/copies/{copy_id}/image")
    def image(run_id: str, copy_id: str, session=Depends(authenticated)):
        run = store.run(run_id)
        item = store.copies(run).get(_name(copy_id))
        if not item:
            raise HTTPException(404, "Experiment copy not found")
        path = _inside(run, item.get("image_path", ""))
        if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(404, "Copied experiment image is not ready")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/{run_id}/decision")
    def decision(run_id: str, body: ExperimentDecision, session=Depends(authenticated)):
        return store.decide(run_id, body, session["reviewer"])

    return router
