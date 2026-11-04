"""Curation: remove near-duplicates, cluster by appearance, keep a diverse, session-aware subset.

Near-duplicates are found two ways (pHash bands, then embedding cosine within a session and globally),
and the sharpest frame of each group is kept. Clusters are k-means on embeddings; from each cluster we
keep the frames that are farthest from each other, with a bonus for rare scene tags (night, rain, glare...).
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter

import numpy as np

from .config import Settings
from .db import Registry
from .embed import embed_paths
from .quality import near_duplicate_groups
from .duplicate_verification import verify_pair

log = logging.getLogger(__name__)


def kmeans(x: np.ndarray, k: int, seed: int, iters: int = 40) -> np.ndarray:
    """k-means++ on L2-normalised vectors. Returns labels."""
    rng = np.random.default_rng(seed)
    n = len(x)
    k = max(1, min(k, n))
    centers = [x[rng.integers(n)]]
    d2 = ((x - centers[0]) ** 2).sum(1)
    for _ in range(1, k):
        probs = d2 / d2.sum() if d2.sum() > 0 else np.full(n, 1 / n)
        c = x[rng.choice(n, p=probs)]
        centers.append(c)
        d2 = np.minimum(d2, ((x - c) ** 2).sum(1))
    C = np.stack(centers)
    labels = np.zeros(n, int)
    x2 = (x * x).sum(1)[:, None]
    for it in range(iters):
        dist = x2 - 2.0 * (x @ C.T) + (C * C).sum(1)[None, :]   # no (n, k, d) temporary
        new = dist.argmin(1)
        if it > 0 and (new == labels).all():
            break
        labels = new
        for j in range(k):
            members = x[labels == j]
            C[j] = members.mean(0) if len(members) else x[rng.integers(n)]
    return labels


def farthest_first(x: np.ndarray, k: int, weights: np.ndarray, seed: int) -> list[int]:
    """Greedy max-min selection, starting from the highest-weight point; weights break near ties."""
    if len(x) <= k:
        return list(range(len(x)))
    chosen = [int(np.argmax(weights))]
    d = ((x - x[chosen[0]]) ** 2).sum(1)
    while len(chosen) < k:
        score = d * (1.0 + 0.25 * weights)
        score[chosen] = -1
        nxt = int(np.argmax(score))
        chosen.append(nxt)
        d = np.minimum(d, ((x - x[nxt]) ** 2).sum(1))
    return chosen


RARE_KEYS = ("lighting", "glare", "soft_focus", "weather", "road_type", "synthetic")


def curate(settings: Settings, registry: Registry, target: int | None = None, source: str | None = None) -> dict:
    cfg_q, cfg_c, cfg_e = settings.get("quality", {}), settings.get("curation", {}), settings.get("embedding", {})
    target = int(target or settings.get("target_images", 2000))
    where = "status IN ('ingested','curated','dropped')"
    params: list = []
    if source:
        where += " AND source=?"
        params.append(source)
    rows = registry.images(where, params)
    if not rows:
        return {"candidates": 0}
    by_id = {r["image_id"]: r for r in rows}
    counts: dict = {"candidates": len(rows)}

    def sharpness(i: str) -> float:
        q = json.loads(by_id[i]["quality"] or "{}")
        return float(q.get("lap_var", 0.0))

    # 1) near-duplicates by pHash
    hashes = {r["image_id"]: int(r["phash"], 16) for r in rows if r["phash"]}
    dropped: dict[str, str] = {}
    for group in near_duplicate_groups(hashes, int(cfg_q.get("phash_near_dup", 6))):
        keep = max(group, key=sharpness)
        for i in group:
            if i != keep and verify_pair(dict(by_id[i]), dict(by_id[keep]))['duplicate']:
                dropped[i] = f"near_duplicate_phash:{keep}"
    counts["near_dup_phash"] = len(dropped)

    # 2) embeddings for the survivors
    alive = {i: by_id[i]["path"] for i in by_id if i not in dropped}
    ids, X, backend = embed_paths(alive, cfg_e.get("backend", "auto"), settings.path("curated_images", "embeddings.npz"),
                                  cfg_e.get("dinov3_model", ""), int(cfg_e.get("image_size", 448)), int(cfg_e.get("batch_size", 16)))
    counts["embedding_backend"] = backend
    # 3) near-duplicates by embedding cosine (blockwise)
    thr = float(cfg_q.get("embed_near_dup", 0.975))
    order = sorted(range(len(ids)), key=lambda j: -sharpness(ids[j]))
    keep_mask = np.ones(len(ids), bool)
    Xo = X[order] if len(ids) else X
    for a in range(len(order)):
        if not keep_mask[order[a]]:
            continue
        sims = Xo[a + 1:] @ Xo[a]
        for off in np.nonzero(sims >= thr)[0]:
            j = order[a + 1 + off]
            if keep_mask[j] and verify_pair(dict(by_id[ids[j]]), dict(by_id[ids[order[a]]]))['duplicate']:
                keep_mask[j] = False
                dropped[ids[j]] = f"near_duplicate_embedding:{ids[order[a]]}"
    counts["near_dup_embedding"] = int((~keep_mask).sum())
    ids = [i for i, k in zip(ids, keep_mask) if k]
    X = X[keep_mask] if len(keep_mask) else X

    # 4) clusters + diverse selection with rare-tag bonus
    tag_counts: Counter = Counter()
    tag_of: dict[str, list[str]] = {}
    for i in ids:
        tags = registry.tags(by_id[i])
        keys = [f"{k}={v['value']}" for k, v in tags.items() if k in RARE_KEYS]
        tag_of[i] = keys
        tag_counts.update(keys)
    n = len(ids)
    weights = np.array([sum(cfg_c.get("rare_tag_bonus", 1.0) * (1.0 - tag_counts[t] / max(1, n)) for t in tag_of[i]) for i in ids]) if n else np.zeros(0)
    k = max(1, math.ceil(n / float(cfg_c.get("cluster_size_target", 25)))) if n else 0
    labels = kmeans(X, k, int(settings.get("seed", 0))) if n else np.zeros(0, int)
    per_cluster = int(cfg_c.get("images_per_cluster", 8))
    selected: list[str] = []
    ranked_rest: list[tuple[float, str]] = []
    for c in range(k):
        members = np.nonzero(labels == c)[0]
        if not len(members):
            continue
        pick = farthest_first(X[members], per_cluster, weights[members], int(settings.get("seed", 0)))
        chosen = {int(members[p]) for p in pick}
        selected += [ids[j] for j in chosen]
        ranked_rest += [(float(weights[j]), ids[j]) for j in members if j not in chosen]
    # top up from the remainder (rarest first) or trim (keep rarest/most diverse) to hit the target
    if len(selected) < target:
        ranked_rest.sort(reverse=True)
        selected += [i for _, i in ranked_rest[: target - len(selected)]]
    elif len(selected) > target:
        w = {ids[j]: weights[j] for j in range(n)}
        selected = sorted(selected, key=lambda i: (-w[i], i))[:target]
    chosen_set = set(selected) | {i for i in by_id if by_id[i]["locked"]}   # reviewed images always stay
    for i in chosen_set:
        dropped.pop(i, None)
    with registry.tx() as conn:
        for idx, i in enumerate(ids):
            status = "curated" if i in chosen_set else "dropped"
            conn.execute("UPDATE images SET status=?, cluster=?, reject_reason=? WHERE image_id=?",
                         (status, int(labels[idx]), None if status == "curated" else "not_selected_diversity", i))
        for i, why in dropped.items():
            conn.execute("UPDATE images SET status='dropped', reject_reason=? WHERE image_id=?", (why, i))
    counts.update(clusters=k, curated=len(chosen_set), dropped=len(by_id) - len(chosen_set),
                  sessions=len({by_id[i]["session_id"] for i in chosen_set}))
    return counts
