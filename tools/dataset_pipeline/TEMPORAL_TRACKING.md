# UFLDv2 temporal tracking experiment

This offline experiment compares four outputs from the same decoded frames:
`raw`, `ema`, `kalman`, and `flow_kalman`. It changes neither model training nor
the iPhone app. Motion-predicted lanes are experimental display candidates and
must not become reviewed labels or drive warnings without separate validation.

The tracker associates boundaries by shared geometry rather than detector list
position. It uses position/velocity states at fixed image rows, gates inconsistent
measurements, preserves unsupported gaps, and expires each row 250 ms after its
last real observation. New tracks require two detections. Capture discontinuities,
explicit scene cuts, major appearance changes, clip changes and frame-size
changes reset tracking. Optical flow uses forward/backward consistency and RANSAC
near supported lanes; failed alignment falls back to the Kalman motion model.
Available vehicle boxes exclude moving objects from alignment.

These initial noise/association settings are uncalibrated experiment defaults.
Do not infer vehicle distance or real-world road geometry from the image state.

## Run

From `tools/dataset_pipeline`, use the existing GPU environment's Python for
checkpoint inference. The demo and cached-trace comparison need only NumPy,
SciPy and OpenCV.

```powershell
# Synthetic validation of plumbing; this is not real driving evidence.
python -m bcdata.ufld_temporal --demo --output ../../reports/temporal-demo

# Infer on a local video with trusted local weights. CPU is the default.
.gpu-venv/Scripts/python.exe -m bcdata.ufld_temporal --video C:/clips/drive.mp4 --checkpoint ../../BrakeCheck_Dataset/models/ufld_baseline/run-20261022-160553-converted-cleared-repairs/best.pt --output ../../reports/temporal-drive --max-frames 300

# Replay identical cached detections and frames, with no model execution.
python -m bcdata.ufld_temporal --trace ../../reports/temporal-drive/detections.jsonl --output ../../reports/temporal-drive-replay
```

`--device cuda` uses the existing shared GPU lock with no waiting, so it cannot
compete with an active training job. `--decoder` explicitly selects stock or one
of the existing short-span policies; stock is the default. No pretrained model
is downloaded. Checkpoint compatibility and vendor model revision are checked.
Existing output directories are refused. Failed runs contain `FAILED.txt` and
must not be treated as completed reports.

## Artifacts and interpretation

Each run saves `report.json`, a four-panel `comparison.mp4`, preview PNGs,
per-frame `comparison.jsonl`, and replayable `detections.jsonl` with cached PNG
frames. Green denotes measured output, amber predicted parts, and gray reference
geometry when supplied. The preview uses 30 sampled frames per playback second;
source timestamps in the trace/report are authoritative. It is not a timed
simulation of a video sampled with `--stride`.

All modes receive identical neural outputs. Inference runs once per sampled
frame. Raw preserves original decoder points; filtered outputs use supported
row samples and do not connect gaps. Recorded tracker latency excludes inference,
frame caching, rendering, and metric computation. It is a desktop measurement,
not iPhone end-to-end or thermal validation.

Without per-frame references, accuracy fields are null. With references the
report includes raster F1, horizontal error, ground-truth-relative residual
change, consecutive-frame ID switches, reacquisition delay, and outputs on empty
reference frames. Empty reference lists explicitly mean no lanes. Missing
`reference` means unscored, not a negative image. Unmatched lanes count as errors,
including stale predictions. Residual-change RMS is measured only for consecutive
matched frames and must be assessed alongside misses/false positives; a model
could otherwise look stable by avoiding hard lanes. ID switches require explicit
reference IDs and are unavailable for raw detections.

## Cached trace format

One JSON object per frame, normalized full-frame coordinates with top-left origin:

```json
{"clip_id":"drive-01","time":0.033333,"image":"frames/000001.png","lanes":[{"points":[[0.3,0.5],[0.31,0.53],[0.32,0.56]],"confidence":0.9}],"reference":[{"id":"boundary-A","points":[[0.3,0.5],[0.31,0.53],[0.32,0.56]]}],"boxes":[[0.4,0.4,0.6,0.7]],"scene_cut":false}
```

`image`, `reference`, `confidence`, `boxes`, and `scene_cut` are optional.
Relative image paths resolve against the trace directory. Scores are not treated
as calibrated probabilities; video inference currently uses the common default
measurement noise, avoiding an invented score calibration. Track identity means
physical boundary identity, not a permanent left/right lane role. Lane changes
must reassign ego-relative roles in a future app integration.

For deployment decisions, use permitted held-out clips split by recording
session. Include curves, lane changes, exits, merges, trucks, camera bumps, night
and rain. Tune on validation and reserve test clips for the final comparison.
Assess warning/lead-selection errors and device latency separately before
integrating filtered output into the app.
