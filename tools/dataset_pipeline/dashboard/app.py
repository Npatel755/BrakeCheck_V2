"""Local dashboard: `streamlit run dashboard/app.py` (reads the registry; never writes to it)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bcdata.config import load_settings  # noqa: E402
from bcdata.licenses import LicenseRegistry  # noqa: E402

st.set_page_config(page_title="BrakeCheck dataset", layout="wide")
settings = load_settings()
db = settings.db_path
if not db.exists():
    st.error(f"No registry at {db}. Run `python -m bcdata init` first.")
    st.stop()
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
q = lambda sql, *a: pd.read_sql_query(sql, con, params=a)

images = q("SELECT image_id, source, session_id, split, status, pool, synthetic, review_status, tags, quality FROM images")
qc = q("SELECT image_id, tier, score, priority, issues FROM qc")
ann = q("SELECT image_id, kind, producer, model_version FROM annotations")
df = images.merge(qc, on="image_id", how="left")
df["tags"] = df["tags"].fillna("{}").map(json.loads)
for key in ("lighting", "glare", "curve", "crosswalk_or_stop_line", "occlusion", "faded_markings", "lane_count"):
    df[key] = df["tags"].map(lambda t: str(t.get(key, {}).get("value", "")))

with st.sidebar:
    st.header("Filters")
    src = st.multiselect("Source", sorted(df.source.unique()), default=sorted(df.source.unique()))
    status = st.multiselect("Status", sorted(df.status.unique()), default=[s for s in df.status.unique() if s == "curated"] or list(df.status.unique()))
    tiers = st.multiselect("QC tier", sorted(df.tier.dropna().unique()), default=sorted(df.tier.dropna().unique()))
    light = st.multiselect("Lighting", sorted(df.lighting.unique()), default=sorted(df.lighting.unique()))
    review = st.multiselect("Review status", sorted(df.review_status.unique()), default=sorted(df.review_status.unique()))
    versions = sorted(ann[ann.kind == "teacher"].model_version.unique())
    version = st.selectbox("Has output from model version", ["(any)"] + versions)
f = df[df.source.isin(src) & df.status.isin(status) & df.lighting.isin(light) & df.review_status.isin(review)]
f = f[f.tier.isin(tiers) | f.tier.isna()] if tiers else f
if version != "(any)":
    f = f[f.image_id.isin(ann[ann.model_version == version].image_id)]

st.title("BrakeCheck lane dataset")
c = st.columns(6)
c[0].metric("Images registered", len(images))
c[1].metric("Curated (unique kept)", int((images.status == "curated").sum()))
c[2].metric("Rejected", int((images.status == "rejected").sum()))
c[3].metric("Auto-annotated", int(ann[ann.kind == "geometry"].image_id.nunique()))
reviewed = images.review_status.isin(["approved", "corrected", "rejected"])
c[4].metric("Human-reviewed", int(reviewed.sum()))
rev = q("SELECT decision, seconds FROM reviews")
c[5].metric("Correction rate", f"{(rev.decision == 'corrected').mean():.0%}" if len(rev) else "-")

st.subheader("Selection")
a, b, d = st.columns(3)
a.bar_chart(f.groupby("source").size(), horizontal=True)
b.bar_chart(f.groupby("lighting").size(), horizontal=True)
d.bar_chart(f.groupby("tier").size(), horizontal=True)
a2, b2, d2 = st.columns(3)
a2.bar_chart(f.groupby("split").size(), horizontal=True)
b2.bar_chart(f.groupby("lane_count").size(), horizontal=True)
d2.bar_chart(f.groupby("review_status").size(), horizontal=True)

st.subheader("Review")
if len(rev):
    r1, r2 = st.columns(2)
    r1.bar_chart(rev.groupby("decision").size())
    r2.metric("Mean seconds per image", f"{rev.seconds.dropna().mean():.0f}" if rev.seconds.notna().any() else "-")
    st.caption(f"Rejection rate {(rev.decision == 'rejected').mean():.0%} · acceptance {(rev.decision == 'approved').mean():.0%}")
else:
    st.caption("No reviews imported yet.")

st.subheader("Processing")
runs = q("SELECT stage, counts, started, finished, ok FROM runs ORDER BY id DESC LIMIT 30")
runs["seconds"] = (runs.finished - runs.started).round(1)
st.dataframe(runs[["stage", "seconds", "ok", "counts"]], use_container_width=True, hide_index=True)

g1, g2 = st.columns(2)
with g1:
    st.markdown("**GPU**")
    if shutil.which("nvidia-smi"):
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        st.code(out or "nvidia-smi returned nothing")
    else:
        st.caption("nvidia-smi not found (CPU machine).")
with g2:
    st.markdown("**Disk**")
    total = sum(p.stat().st_size for p in settings.root.rglob("*") if p.is_file())
    free = shutil.disk_usage(settings.root).free
    st.write(f"Dataset folder {total / 1e9:.2f} GB · free {free / 1e9:.0f} GB")

st.subheader("Licences")
lic = pd.DataFrame(LicenseRegistry(settings.licenses_path).table())
st.dataframe(lic, use_container_width=True, hide_index=True)
st.caption("Pools: commercial = cleared for the App Store model; share_alike = ZOD (CC BY-SA); quarantine = not cleared.")

st.subheader("Images")
st.dataframe(f[["image_id", "source", "split", "tier", "score", "priority", "review_status", "lighting", "lane_count", "issues"]]
             .sort_values("priority", ascending=False), use_container_width=True, hide_index=True)
overlay_dir = settings.path("reports", "overlays")
pick = st.selectbox("Show overlay", ["(none)"] + f.image_id.tolist()[:500])
if pick != "(none)":
    p = overlay_dir / f"{pick}.png"
    st.image(str(p)) if p.exists() else st.caption("No overlay yet: run `python -m bcdata overlays`.")
