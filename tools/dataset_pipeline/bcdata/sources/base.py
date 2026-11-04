"""Shared ingestion helpers: download gate, resumable downloads, and registering one image."""
from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import Settings
from ..db import Registry
from ..licenses import LicenseRegistry
from ..quality import assess, phash, read_rgb, sha256_file

log = logging.getLogger(__name__)
UA = {"User-Agent": "brakecheck-dataset/0.1 (+https://github.com/Npatel755/BrakeCheck_V2)"}


class ApprovalRequired(RuntimeError):
    pass


def check_download_budget(settings: Settings, planned_bytes: int, dest: Path, approved_gb: float | None) -> None:
    """Refuse downloads above the approval threshold unless the caller passed an explicit approval."""
    gb = planned_bytes / 1e9
    dest.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(dest).free / 1e9
    need = gb + float(settings.get("download.min_free_disk_gb", 20.0))
    threshold = float(settings.get("download.approval_threshold_gb", 5.0))
    log.info("planned download %.2f GB into %s (free %.1f GB)", gb, dest, free_gb)
    if free_gb < need:
        raise ApprovalRequired(f"not enough disk: need {need:.1f} GB free at {dest}, have {free_gb:.1f} GB")
    if gb > threshold and (approved_gb is None or approved_gb + 1e-9 < gb):
        raise ApprovalRequired(
            f"download is {gb:.2f} GB (> {threshold} GB). Destination {dest}, needs ~{need:.1f} GB free "
            f"(have {free_gb:.1f}). Re-run with --approve-download-gb {gb:.1f} once approved.")


def git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha1(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path, expected_size: int | None = None, git_sha: str | None = None,
             registry: Registry | None = None, retries: int = 4, session: requests.Session | None = None) -> Path:
    """Resumable download to `dest` (via dest.part). Verifies size and, for GitHub files, the git blob hash."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (expected_size is None or dest.stat().st_size == expected_size):
        if git_sha is None or git_blob_sha1(dest) == git_sha:
            return dest
        dest.unlink()
    part = dest.with_suffix(dest.suffix + ".part")
    http = session or requests
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        headers = dict(UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with http.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:  # already complete
                    break
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for block in r.iter_content(1 << 20):
                        f.write(block)
            break
        except (requests.RequestException, OSError) as exc:
            wait = 2 ** attempt
            log.warning("download %s failed (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    else:
        raise IOError(f"failed to download {url}")
    if expected_size is not None and part.stat().st_size != expected_size:
        part.unlink(missing_ok=True)
        raise IOError(f"{url}: size {part.stat().st_size if part.exists() else 0} != expected {expected_size}")
    if git_sha is not None and git_blob_sha1(part) != git_sha:
        part.unlink(missing_ok=True)
        raise IOError(f"{url}: git blob hash mismatch")
    part.replace(dest)
    if registry is not None:
        with registry.tx() as c:
            c.execute("INSERT OR REPLACE INTO downloads (url, dest, bytes, sha256, completed_at) VALUES (?,?,?,?,?)",
                      (url, str(dest), dest.stat().st_size, None, time.time()))
    return dest


def requalify(settings: Settings, registry: Registry) -> dict:
    """Re-run the quality checks with the current thresholds (after tuning them). Rejected frames that now
    pass come back as 'ingested'; newly failing uncurated frames become 'rejected'. Reviewed images are left alone."""
    counts = {"checked": 0, "restored": 0, "newly_rejected": 0}
    for r in registry.images("locked=0 AND status IN ('ingested','rejected','dropped','curated')"):
        rgb = read_rgb(r["path"])
        rep = assess(rgb, settings.get("quality", {}))
        counts["checked"] += 1
        fields: dict = {"quality": rep.metrics}
        if rep.ok and r["status"] == "rejected":
            fields.update(status="ingested", reject_reason=None, phash=format(phash(rgb), "016x"))
            counts["restored"] += 1
        elif not rep.ok and r["status"] in ("ingested", "dropped"):
            fields.update(status="rejected", reject_reason=rep.reason)
            counts["newly_rejected"] += 1
        registry.update_image(r["image_id"], **fields)
        tags = registry.tags(registry.image(r["image_id"]))
        for k in ("lighting", "glare", "soft_focus"):
            if tags.get(k, {}).get("origin") == "heuristic":
                tags.pop(k)
        tags.update({k: {"value": v, "origin": "heuristic"} for k, v in rep.tags.items() if k not in tags})
        registry.update_image(r["image_id"], tags=tags)
    return counts


@dataclass
class IngestResult:
    image_id: str | None
    status: str          # added | duplicate | rejected
    reason: str | None = None


def register_image(settings: Settings, registry: Registry, licenses: LicenseRegistry, path: Path, *,
                   source: str, session_id: str, source_ref: str, frame_index: int | None = None,
                   synthetic: bool = False, extra_tags: dict | None = None) -> IngestResult:
    """Validate one image file and add it to the registry. Exact duplicates are skipped by content hash."""
    sha = sha256_file(path)
    existing = registry.image_by_sha(sha)
    if existing is not None:
        return IngestResult(existing["image_id"], "duplicate", "exact_duplicate")
    pool = licenses.pool_for_source(source)
    image_id = f"{source}-{sha[:16]}"
    rgb = read_rgb(path)
    report = assess(rgb, settings.get("quality", {}))
    tags = dict(report.tags)
    if extra_tags:
        tags.update(extra_tags)
    row = dict(image_id=image_id, source=source, session_id=session_id, source_ref=source_ref,
               frame_index=frame_index, path=str(path), width=report.metrics.get("width"),
               height=report.metrics.get("height"), sha256=sha, license_id=source, pool=pool,
               synthetic=int(synthetic), quality=report.metrics,
               tags={k: {"value": v, "origin": "heuristic" if k not in (extra_tags or {}) else "source"} for k, v in tags.items()})
    if not report.ok:
        row.update(status="rejected", reject_reason=report.reason)
        registry.add_image(**row)
        return IngestResult(image_id, "rejected", report.reason)
    row["phash"] = format(phash(rgb), "016x")
    registry.add_image(**row)
    return IngestResult(image_id, "added")
