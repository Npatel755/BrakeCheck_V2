# Optional flagged-image teacher experiment

This experiment leaves the original flagged conversions, human labels, source
masks, regular exports, existing trained models and production training status
in place. It never inserts experimental copy IDs into the main registry.

Start with `Start-Flagged-Model-Trial.cmd`. Resume completed stages with
`Start-Flagged-Model-Trial.cmd -Resume`. The launcher refuses a second running
worker and writes independent stdout/stderr logs beside the run directory.

The current frozen reference is
`models/ufld_baseline/run-20261020-142147-post-n62-label-fixes`: **13,086 cleared
training images, 1,586 validation images and 1,756 reserved test IDs**. Every
cleared training example remains in the experimental UFLDv2 snapshot, including
the verified negative examples. Validation images, targets and reserved test IDs
remain unchanged. Additional photos can only enter the training split and cannot
share a held-out ID, original image fingerprint or recording session.

The automatic stages are:

1. Freeze a read-only registry snapshot, the current pending flagged cohort, the
   cleared dataset fingerprints and original conversion geometry.
2. Adapt the pinned MIT comma road/paint segmenter from the locally adapted
   checkpoint, using the current cleared TRAIN source masks. New fitting never
   uses pseudo-labels. Fixed Audi VAL paint IoU selects the weights; if adaptation
   fails to improve that metric, preserve the original parent weights. Record all
   local teacher fitting ancestry, including the parent's prior train images.
3. Create independent full-resolution JPEG quality-97 replicas of the flagged
   images and exact frozen copies of their conversion JSON. Keep both original
   and derived image fingerprints. Existing JPEGs can be copied byte-for-byte.
   No originals are recompressed or changed. This avoids duplicating approximately
   74 GB of source PNGs when C: has approximately 47 GB available.
4. Predict paint from each replica's image only, including a second horizontal
   flip pass. No source mask or original conversion guides inference. Store every
   proposal separately, including quality failures and held-out comparison cases.
   Confidence, flip agreement and valid physical painted geometry gate additions.
   Model absence never becomes a human-verified no-lanes label. Road edges are
   not painted lane training targets.
5. Freeze a new dataset with **all original cleared training data plus eligible
   experimental TRAIN copies**. Exclude experimental reviewer skips/rejections
   recorded before this freeze. Default extra count is capped at the cleared
   TRAIN count (at most 1:1 pseudo to cleared); all copies still receive proposals.
6. Automatically run another 20-epoch UFLDv2 benchmark, warm-started from the
   cleared baseline `best.pt`. Evaluate the untouched warm-start before updates
   and keep the best stock-decoder checkpoint, including those starting weights.
   Use a separate progress file and never promote weights into the app.

The first frozen cohort has **23,367 copied proposal candidates**: **21,087 TRAIN,
1,107 VAL and 1,173 TEST**. VAL/TEST copies are comparison artifacts only; the
UFLDv2 trainer never loads them. The final extra training count depends on quality
checks, optional experiment decisions, duplicate guards and the ratio cap.

Open `http://127.0.0.1:8765/experiment`, or **Model experiment** in the reviewer.
It works with the existing paired-phone authentication. It shows the original
flagged conversion and the new model proposal side by side, with Approve,
Exclude and Skip. Decisions live only in `<run>/review.sqlite` and cannot change
original human decisions or override a failed proposal quality gate. Once the
training snapshot is frozen, later decisions apply only to a future rebuild.

Artifacts live under `BrakeCheck_Dataset/experiments/flagged_teacher/run-*`:

- `experiment.json`: stage, counts, immutable input references and worker PID.
- `registry_snapshot.sqlite`, `inventory.json`: read-only original state.
- `teacher/prepare_status.json`, `teacher/status.json`: mask preparation/training.
- `teacher/teacher.json`: selected weights, metrics, ancestry and preprocessing.
- `copies.json`, `copies/`, `proposals/`: isolated replicas and original/model geometry.
- `selection.json`: experiment decisions used when freezing the dataset.
- `ufld_run/dataset/experiment.json`: exact cleared/extra counts and exclusions.
- `ufld_training.json`, `ufld_run/status.json`: isolated UFLDv2 progress.
- `comparison.json`: old/new best F1 on the same frozen validation data.
- `preservation.json`: read-only end-of-run original-state drift check. Concurrent
  legitimate reviewer/import changes are reported, never restored or overwritten.

Restarting an interrupted teacher preparation preserves its partial files in a
separate `teacher-attempt-*` directory and starts a fresh preparation. A completed
teacher/proposal/export stage is reused. If teacher fitting is interrupted, its
adaptation restarts from the frozen parent; UFLDv2 resumes its saved `last.pt`.
The current run must exit before the resume launcher starts a worker.

Scores are provisional. Additional optimization and the added pseudo-data can
both affect the result; this is not a matched-training-budget causal comparison.
The upstream comma model may already have seen comma validation images, so its
comma scores are diagnostic. Teacher selection uses Audi validation drives.
Existing dataset notices and local-development license conditions still apply;
this experiment does not grant new redistribution rights.
