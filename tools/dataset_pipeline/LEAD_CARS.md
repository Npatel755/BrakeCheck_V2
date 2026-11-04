# Lead-car labeling in BrakeCheck Studio

Open **Lead cars** in Studio, or `http://127.0.0.1:8765/lead-cars`.
The first collection contains 500 local comma10k photos screened for vehicle
candidates using the pinned RTMDet-tiny weights already used by the app.
Detector output is comparison evidence, never a human label or a confirmed lead.

Draw one tight box around the visible body of the nearest vehicle ahead in the
lane the camera vehicle follows. Include stopped traffic, trucks, buses and
motorcycles. Exclude parked, oncoming and adjacent-lane vehicles. Do not guess
hidden vehicle extent. A clipped vehicle stops at the image border. If the photo
does not establish lane membership, use **Unclear / skip**. **No lead vehicle**
records an explicit negative; pending or skipped photos never become negatives.

Drag across the car to draw. Drag inside the box to move it, or drag a corner
to resize. Zoom is available for distant vehicles; **Pan** lets you move around
an enlarged image. **Undo box** affects the draft. **Undo saved label** restores
your previous saved decision if nobody has changed that image since. Drafts
survive a page reload; a save is only reported successful after the PC confirms.
If a save is interrupted, retry the same pending request. Revision and source
checks reject concurrent or stale overwrites. Each tab/phone gets its own claim.

The separate SQLite store is
`BrakeCheck_Dataset/experiments/lead_cars/review.sqlite`. It preserves immutable
human events and uses independent task records; it never modifies lane reviews,
source masks, checkpoints or the original dataset registry.

## Curation and growth

The first 500 photos come from 500 different comma10k recording sessions in
the original **training** pool. Original validation/test identities, recording
sessions, exact hashes and perceptual near duplicates are excluded. Rejected
photos, source duplicates and near duplicates inside this collection are also
excluded. The photos are copied and hash-verified, retaining source references,
MIT licence text, detector recipe/weight hash and all candidate boxes.

A stable session hash assigns the new lead task's own training/validation
partition. Initial counts are 406 train and 94 validation. This does not move
any original training, validation or test memberships. All photos need human
decisions, including those assigned to lead validation. No original test image
is used in curation or fitting.

Double-click `Add-Lead-Car-Images.cmd` in the project root to add 250 photos.
It reuses exact cached inference, screens more photos if needed and appends a
new immutable batch. Existing boxes, event history and split assignments are
preserved. For another size:

```powershell
./tools/dataset_pipeline/Expand-Lead-Car-Images.ps1 -Add 500
```

The curation worker uses the existing WSL detector environment at
`/home/nick/brakecheck-detector-env`. The reviewer uses its normal Windows
environment. Source photos and labels remain on this PC.

## Exports

**Export saved labels** creates a versioned export under
`experiments/lead_cars/exports/<content-hash>/`. It includes original-coordinate
boxes, no-lead decisions, candidate detections, source/session/split/licence
provenance, immutable human events, and train/validation COCO files. Skipped
and pending photos are omitted. Export never starts training or changes an app.

For a candidate-ranking model, boxes matching the human lead at IoU >= 0.5 are
positive candidates. Explicit no-lead images supply negative candidates. If the
detector missed the human box, the image is marked `detector_missed_lead` and
is **not** converted into a set of false negative candidate labels. Its human
box remains available to improve the detector or test missed-lead handling.

Still images cannot establish motion or temporal switch quality. Later video
validation must measure wrong-lead selections, false acquisitions, loss duration,
switch delay and sustained phone cost alongside image-level metrics.

## External dataset requirement

For additional external lead training datasets, use only sources that already
annotate which vehicle is the lead. Generic vehicle boxes and automatic
center-of-image selections do not satisfy this requirement. The existing
500-photo manual queue remains available for the user's own lead labels.
See `reports/lead-cars-20261024/preannotated-datasets.md` for the verified
annotation shortlist and unresolved import requirements. No shortlisted
external dataset has been imported or cleared for training yet.

## Training the reviewed collection

The 2026-10-24 user-authorized run uses immutable export
`f53cb0f82f833f954ee9` (362 lead boxes and 138 explicit no-lead reviews).
The original 94-session lead validation split stays out of fitting, epoch
selection and threshold tuning. Of the other 406 photos, 267 have usable
detector candidate targets; 222 fit and 45 internal-development photos are
assigned by a fixed recording-session hash. The other 139 training photos
contain a human lead the detector does not match at IoU >= 0.5. They remain
detector-improvement examples, never false all-negative training samples.

Two local experiments use these same assignments: a 1.1M-parameter
MobileNetV3-Small scene/ROI scorer trained from scratch, and a compact learned
geometry scorer. Neither downloads weights or trains on external datasets.
The geometry scorer cannot see road/lane pixels and is a comparator, not proof
of lane understanding. Select models using internal-development loss; do not
re-tune them on the original validation results.

To run a new visual experiment from a fresh reviewed export (choose a new output
folder; an existing run is never overwritten):

```powershell
$env:PYTHONPATH='tools/dataset_pipeline'
tools/dataset_pipeline/.gpu-venv/Scripts/python.exe -m bcdata.lead_training `
  --manifest BrakeCheck_Dataset/experiments/lead_cars/exports/EXPORT_ID/manifest.json `
  --root BrakeCheck_Dataset/experiments/lead_cars `
  --out BrakeCheck_Dataset/experiments/lead_cars/runs/NEW_RUN --epochs 80
```

Live status and immutable model/metric artifacts are under `runs/`. Studio's
Lead cars page shows the latest recorded worker progress. Saving a label does
not itself launch another training run or replace the iPhone model.
