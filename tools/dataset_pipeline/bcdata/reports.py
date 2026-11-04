"""Review statistics and the final dataset report, built only from what is in the registry and on disk."""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from .config import Settings
from .db import Registry
from .licenses import LicenseRegistry
from .qc import calibration


def review_stats(registry: Registry) -> dict:
    rows = registry.conn.execute("""SELECT r.*, i.source, i.tags, q.tier FROM reviews r JOIN images i ON i.image_id=r.image_id
                                    LEFT JOIN qc q ON q.image_id=r.image_id""").fetchall()
    total = len(rows)
    dec = Counter(r["decision"] for r in rows)
    secs = [r["seconds"] for r in rows if r["seconds"]]
    by_tag: dict[str, Counter] = defaultdict(Counter)
    by_reviewer: dict[str, Counter] = defaultdict(Counter)
    by_tier: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_reviewer[r["reviewer"]][r["decision"]] += 1
        by_tier[r["tier"] or "?"][r["decision"]] += 1
        for k, v in json.loads(r["tags"] or "{}").items():
            by_tag[f"{k}={v['value']}"][r["decision"]] += 1

    def rates(c: Counter) -> dict:
        n = sum(c.values())
        return {"n": n, **{d: round(c[d] / n, 3) for d in ("approved", "corrected", "rejected", "difficult")}} if n else {"n": 0}
    return {
        "reviews": total,
        "acceptance_rate": round(dec["approved"] / total, 3) if total else None,
        "correction_rate": round(dec["corrected"] / total, 3) if total else None,
        "rejection_rate": round(dec["rejected"] / total, 3) if total else None,
        "difficult_rate": round(dec["difficult"] / total, 3) if total else None,
        "mean_seconds_per_image": round(sum(secs) / len(secs), 1) if secs else None,
        "by_reviewer": {k: rates(v) for k, v in by_reviewer.items()},
        "by_qc_tier": {k: rates(v) for k, v in by_tier.items()},
        "errors_by_scene_tag": {k: rates(v) for k, v in sorted(by_tag.items()) if sum(v.values()) >= 3},
        "calibration": calibration(registry),
    }


def dataset_counts(registry: Registry) -> dict:
    q = lambda sql, *a: registry.conn.execute(sql, a).fetchall()
    return {
        "images_by_status": {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM images GROUP BY status")},
        "curated_by_source": {r[0]: r[1] for r in q("SELECT source, COUNT(*) FROM images WHERE status='curated' GROUP BY source")},
        "curated_by_split": {r[0] or "none": r[1] for r in q("SELECT split, COUNT(*) FROM images WHERE status='curated' GROUP BY split")},
        "curated_by_pool": {r[0]: r[1] for r in q("SELECT pool, COUNT(*) FROM images WHERE status='curated' GROUP BY pool")},
        "sessions_curated": registry.count("SELECT COUNT(DISTINCT session_id) FROM images WHERE status='curated'"),
        "rejects": {r[0]: r[1] for r in q("SELECT reject_reason, COUNT(*) FROM images WHERE status='rejected' GROUP BY reject_reason")},
        "drops": {(r[0] or "").split(":")[0]: r[1] for r in q("SELECT reject_reason, COUNT(*) FROM images WHERE status='dropped' GROUP BY reject_reason")},
        "annotations": {f"{r[0]}:{r[1]}": r[2] for r in q("SELECT kind, producer, COUNT(*) FROM annotations GROUP BY kind, producer")},
        "qc_tiers": {r[0]: r[1] for r in q("SELECT q.tier, COUNT(*) FROM qc q JOIN images i ON i.image_id=q.image_id WHERE i.status='curated' GROUP BY q.tier")},
        "review_status": {r[0]: r[1] for r in q("SELECT review_status, COUNT(*) FROM images WHERE status='curated' GROUP BY review_status")},
        "models": [dict(r) for r in q("SELECT model_version, teacher, promoted, metrics FROM models")],
    }


def scene_diversity(registry: Registry) -> dict:
    c: Counter = Counter()
    for r in registry.images("status='curated'"):
        for k, v in registry.tags(r).items():
            if k in ("lighting", "glare", "soft_focus", "curve", "merge_or_split", "crosswalk_or_stop_line",
                     "occlusion", "faded_markings", "lane_count", "synthetic", "weather", "road_type"):
                c[f"{k}={v['value']}"] += 1
    return dict(sorted(c.items()))


def final_report(settings: Settings, registry: Registry, extra: dict | None = None) -> Path:
    lic = LicenseRegistry(settings.licenses_path)
    counts = dataset_counts(registry)
    review = review_stats(registry)
    div = scene_diversity(registry)
    evals = sorted(settings.path("reports").glob("eval_*.json"))
    last_eval = json.loads(evals[-1].read_text()) if evals else None
    exports = {}
    for name in ("ufld_validation.json", "ufld_export.json", "seg_export.json"):
        p = settings.path("reports", name)
        if p.exists():
            exports[name] = json.loads(p.read_text())
    open_cases = registry.conn.execute("""SELECT i.image_id, q.tier, q.priority, q.issues FROM images i JOIN qc q ON q.image_id=i.image_id
                                          WHERE i.status='curated' AND i.locked=0 AND q.tier IN ('low','invalid')
                                          ORDER BY q.priority DESC LIMIT 25""").fetchall()
    used_sources = sorted({r[0] for r in registry.conn.execute("SELECT DISTINCT source FROM images")})
    L = [f"# BrakeCheck lane dataset report", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{registry.path}`.", "",
         "Automatic labels are provisional. Only `label_origin=human` rows in the export manifests have been checked by a person.", "",
         "## Dataset counts", "", "```json", json.dumps(counts, indent=1, default=str), "```", "",
         "## Sources and licence status", "", "| source | licence | status | pool |", "|---|---|---|---|"]
    for s in used_sources:
        e = lic.get(s)
        L.append(f"| {s} | {e.data.get('license', '?')} | {e.status} | {e.pool} |")
    L += ["", "Models:", ""]
    for e in lic.entries.values():
        if e.group == "models":
            L.append(f"- {e.key}: {e.data.get('license', '?')} - {e.status}")
    L += ["", "## Scene diversity (curated)", "", "```json", json.dumps(div, indent=1), "```", "",
          "## Review and annotation error rates", "", "```json", json.dumps(review, indent=1, default=str), "```", ""]
    if last_eval:
        from .evaluate import to_markdown
        L += ["## Model comparison (latest evaluation)", "", to_markdown(last_eval)]
    else:
        L += ["## Model comparison", "", "No evaluation has been run yet (`bcdata evaluate`).", ""]
    L += ["## UFLD V2 export validation", "", "```json", json.dumps(exports, indent=1, default=str) if exports else "not run", "```", "",
          "## Outstanding review cases (top 25 by priority)", ""]
    for r in open_cases:
        L.append(f"- {r['image_id']} ({r['tier']}, priority {r['priority']:.2f}): "
                 f"{', '.join(i['code'] for i in json.loads(r['issues'] or '[]'))}")
    if extra:
        L += ["", "## Run notes", "", "```json", json.dumps(extra, indent=1, default=str), "```"]
    p = settings.path("reports", f"final_report_{time.strftime('%Y%m%d_%H%M%S')}.md")
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p
