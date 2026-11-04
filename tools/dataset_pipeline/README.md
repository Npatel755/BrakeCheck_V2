# BrakeCheck lane dataset pipeline (`bcdata`)

Builds a lane-detection training set for Ultra-Fast-Lane-Detection-v2 (UFLD V2), plus road/lane segmentation masks, from licence-cleared driving images.

The pipeline runs in this order:

1. sources
2. quality checks
3. curation
4. leak-free splits
5. annotation models ("teachers")
6. fusion
7. lane geometry
8. QC and review priority
9. CVAT review
10. exports
11. evaluation
12. active learning

Everything is resumable: re-running a stage skips work that is already done. Every image and label is tracked in `BrakeCheck_Dataset/metadata.sqlite` with its source, licence, model version and review state.

**Automatic labels are provisional.** Only rows with `label_origin=human` in an export's `manifest.csv` have been checked by a person. comma10k masks were drawn by people (`label_origin=source`), but turning them into lane lines is automatic and is spot-checked by QC sampling.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install torch torchvision                        # pick the CUDA build for your GPU
pip install -r requirements.txt
python -m pytest tests -q                            # 23 tests, ~1 min on CPU
python -m bcdata run-all --target 2000               # milestones 1-4 on comma10k (about 1.6 GB download)
streamlit run dashboard/app.py
```

## Commands

| Command | What it does |
|---|---|
| `python -m bcdata init` | Create the `BrakeCheck_Dataset/` tree and the registry. |
| `licenses` | Print the licence registry ([config/licenses.yaml](config/licenses.yaml)). |
| `ingest comma10k --limit N` | Download a session-balanced subset. Every file is checked against its git hash. The human mask is attached. |
| `ingest comma2k19 --path <chunk>` | Extract frames from an already downloaded chunk (FFmpeg, 1 fps, adaptive). `comma2k19-info` explains the torrent. |
| `ingest carla` / `ingest zod` | CARLA simulator renders with ground truth, or ZOD with its own human annotations. Both are optional. |
| `requalify` | Re-run quality checks after changing thresholds. |
| `curate --target 2000` | Remove exact and near-duplicates (pHash, then embeddings), cluster, and keep a diverse set with rare conditions favoured. |
| `split` | Assign train/val/test by recording session. The test split is the fixed evaluation set. |
| `annotate [--teachers a,b]` | Run the enabled teachers one at a time, unloading each from the GPU before the next. |
| `train-teacher [--promote]` | Train the DINOv3 decoder on human masks. |
| `fuse`, `geometry`, `qc` | Fuse the teachers' masks, convert masks to ordered lane lines, then score and prioritise. |
| `overlays --limit 100` | Write visual spot-check images to `reports/overlays/`. |
| `queue` | Next review batch (CSV) plus a ready-to-import CVAT task package. |
| `review-import --batch .. --xml .. --reviewer .. --minutes ..` | Bring reviewed labels back. They are locked from then on. |
| `export-ufld [--ufld-repo PATH]` | CULane-format export, validated. With `--ufld-repo` it is also checked against UFLD's own cache script. |
| `export-seg` | Road/lane semantic segmentation export. |
| `evaluate` | Compare teachers on the fixed evaluation set. |
| `al-cycle` | One active-learning round: fine-tune, evaluate, promote only if better, re-label, build the next queue. |
| `report` | Write the final markdown report in `reports/`. |

## Design rules the code enforces

- **Licences decide pools.**
  - `commercial` holds cleared sources (comma10k, comma2k19, CARLA with attribution).
  - `share_alike` holds ZOD (CC BY-SA 4.0).
  - `quarantine` holds anything unverified.
  - Exports default to `commercial` only.
  - A model marked unverified refuses to run without `--allow-unverified`, and its outputs are quarantined.
- **Downloads over 5 GB stop** and print the size, destination and disk needed. Pass `--approve-download-gb` once approved.
- **Splits stay leak-free.** Sessions are never split across train/val/test. Synthetic images never enter test.
- **Human labels are final.** After a review the image is locked. Teachers, fusion and geometry skip it, and the registry refuses model output for it.
- **No invented lanes.** Dashed gaps are bridged, since the dashes are one painted line, but nothing is extended beyond the last observed paint. Double lines become one lane, as in CULane.
- **QC scores are not probabilities.** `qc.calibration()` reports the measured acceptance rate per score bin once reviews exist. A random 5% of high-confidence images always goes to review.
- **The DINOv3 teacher will not pseudo-label** until a decoder checkpoint has training steps and validation metrics.
- **SAM 2.1 refinement is measured, not assumed.** Its fusion weight is 0 until `evaluate` shows it beats its base model.

## Layout

The authorized public ZOD mirror import is resumed with
`Start-ZOD-Mirror-Import.cmd` at the workspace root. Its isolated collection is
`BrakeCheck_Dataset/experiments/zod_mirror`; `/source-audits?source=zod_mirror`
uses up to three spread-out samples per conversion group. The repaired v2 batch
has 2,420 samples across 1,004 groups covering all 100,000 photos. Groups share marking types, slot count,
converter/coordinate rules and exactly the same warnings. Bad conversion holds
the whole group in Needs repair; editing one photo cannot clear its group.
Original polygons and individual decisions/corrections are preserved. The two
image resolutions share one identity; polygon overlays are off by default.
The main reviewer at `/?queue=zod_audit` includes these audits and independent
pretrained/local road-marking model proposals, with model selection on phones.
The v2 repair lives in `repairs/observed-curves-v2` beneath the isolated ZOD root;
the original database, human votes and corrections remain separate. Original
sample details are readable at `/api/studio/zod/original/samples/{image_id}`.
These share-alike inputs
stay held outside existing training until their source splits and later
experiment integration are resolved.

```
bcdata/            pipeline package (sources/, teachers/, geometry, fusion, qc, cvat, exports, evaluate, active)
config/            pipeline.yaml (all thresholds), licenses.yaml (licence registry)
dashboard/app.py   Streamlit dashboard
tests/             pytest suite + synthetic road generator
tools/             check_ufld_dali.py (GPU check of the export with UFLD's real loader)
BrakeCheck_Dataset/  data root (the folder tree from the spec)
```

See [SETUP_NICK.md](SETUP_NICK.md) for the GPU machine, the gated model access requests, CVAT, and what has and hasn't been verified.

## Automatic copied-proposal experiment

The flagged-image experiment trains the proposal teacher, makes separate image
copies, generates painted-line proposals, and trains UFLDv2 on **every cleared
base training image plus eligible training-split proposals**. Individual human
approvals are optional; the automatic experiment does not wait for them.

Automatic quality checks exclude missing, malformed, uncertain or unstable
predictions. An empty prediction never becomes a verified no-lines example.
Optional human approval can override advisory confidence, span or consistency
holds on valid nonempty painted geometry. That override must match the current
proposal SHA256. Exclude and Skip keep a copy out of the next snapshot.

Snapshots remain fixed after creation. Later optional decisions apply to a
future rebuild. Validation/test sessions never become extra training examples,
original flagged conversions and reviews remain unchanged, and experiment labels
remain `teacher_pseudo`. Approved dataset collections and the app's model are
unchanged until separately promoted.

The frozen registry may include inputs rejected before split assignment. The
experiment excludes only those unsplit rejected inputs from its split inventory;
other missing or invalid assignments still fail validation. No source record or
frozen inventory is rewritten to repair this case.


### ZOD continuous lane paths

The main audit reviewer selects `repairs/continuous-boundaries-v4` once its
complete conversion and proposal staging pass. It joins supported dash gaps
inside observed endpoints, retaining `observed_paint_segments` and labelled
`inferred_sections`. The derived `segments` are the continuous boundary target
used by the renderer/exporter; original painted sections are available through
the gold comparison toggle. V2 human reviews remain in the previous database
and are readable at `/api/studio/zod/previous/samples/{image_id}`. Changed V3
labels require fresh sample audits. This update does not release ZOD into an
existing training collection or restart training.


V4 also joins nearby sections already assigned to the same boundary across
small polygon-tip gaps (at most 1.5% of image height and 2.5% of width).
These proximity joins still pass the occlusion and boundary-order checks.
This avoids rejecting 1?6 pixel breaks because a polygon tip has a noisy
local tangent. Source observations and V3 human history remain preserved.
