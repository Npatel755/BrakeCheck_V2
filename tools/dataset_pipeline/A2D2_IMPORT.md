# Audi A2D2 import

The initial pinned cohort contains 4,000 front-center image/semantic-label pairs
from 23 recording drives (about 14.6 GB). The original, aligned 1920 x 1208
image and mask files remain unchanged. Whole-drive splits contain 3,276 train,
362 validation and 362 reserved test frames before quality and duplicate
exclusions. Temporal spacing and pHash deduplication reduce redundant frames.

Audi provides public downloads without an account or access application:
https://a2d2-dataset.github.io/ . The dataset uses CC BY-ND 4.0. Retain source
attribution and licence notices. This pipeline keeps adapted images/annotations
private for local development; it does not authorize public redistribution of
an adapted dataset or establish unconditional model-distribution clearance.

Run or resume from `tools/dataset_pipeline` in PowerShell:

```powershell
.\.gpu-venv\Scripts\python.exe -m bcdata.import_audi --plan-file ..\..\BrakeCheck_Dataset\reports\a2d2_discovery-20261018-001013\import_plan_4000.json --approve-download-gb 16 --workers 4 --chunk-size 64
```

The model environment supplies the already installed inference dependencies.
The importer explicitly runs the pinned comparison model on CPU with two
threads. It does not acquire the training GPU or modify a running UFLDv2
snapshot. The live progress file is
`BrakeCheck_Dataset/reports/a2d2_import.json`; logs from the launcher are in the
dataset's `logs` directory. Restarting the same command reuses verified original
downloads and staged annotations, and retries hidden failures.

Each stage validates its inputs: pinned object sizes/checksums, exact official
semantic colors, source/geometry file hashes, latest annotations, QC provenance,
independent model comparison and the audit gate. Staged images are hidden from
the phone reviewer until publication. Existing human decisions, claims and
annotation history remain protected. An interrupted publication can resume.

Only the solid-line and dashed-line semantic classes become lane candidates.
Arrows, driving instructions, zebra crossings and road-area pixels are not lane
boundaries. Transverse paint is filtered rather than assigned to UFLDv2 slots.
Road edges remain a separate label type. Source masks remain the reference;
comparison predictions do not silently replace them.

Native-resolution geometry currently retains conservative warnings for real
curve/fragment association problems. Audi-specific source center coverage can
remove broad-mask area bias only when point agreement, component completeness
and slot checks pass. It cannot clear missing-boundary, off-mask or uncertain
association warnings. Scaling all pixel thresholds produced regressions in the
12-image pilot and is not enabled. This pilot is development evidence, not a
dataset accuracy estimate.

The review workflow uses three actual human samples per conversion group when
available, separated by split, repair cause, transverse paint, lighting and lane
count. All samples must be approved unchanged to release the unsampled group
as `source_validated`. A correction or rejection holds that group for repair.
Flagged conversions remain individual tasks. Neither model agreement nor an
assistant inspection creates human approval. Pending, held and flagged images
remain outside the eligible UFLDv2 export. Source-validated provenance is
distinct from individual human review.

Audi licence and attribution files accompany derived local exports. Already
running training retains its frozen cohort; Audi can enter a later snapshot
only after review gates pass. Reserved test drives stay out of training.

## Source conversion repair

The Audi converter `a2d2-piecewise-source-v1` follows measured paint centers
without polynomial bowing. It joins compatible dash fragments only when
tangent, marking type and source support agree. Slot assignment uses shared
observed image rows. Previously supported boundary spans are retained when a
reconstruction would shorten or move them away from valid source paint.
Uncertain joins, crossings and missing source paint remain explicit flags.

Audi-specific QC measures paint-center coverage. A complete short visible
boundary can pass; a truncated boundary cannot. Paint beyond the four supported
lane slots is excluded only when actual overlapping source rows establish both
the outer boundary and its adjacent inner boundary. Model disagreement remains
a flag and does not overwrite source labels.

Repair evaluation separates development images, untouched validation/test
images, actual approved regression controls and failed audit controls. The
sealed evidence report is
`BrakeCheck_Dataset/reports/a2d2_repair-20261018/independent_evaluation/complete-frozen-evaluation.json`.
It contains 36 development cases, 24 untouched evaluation cases, 103 approved
controls and 3 failed audit controls. There are no detected approved-control
geometry regressions. These are diagnostic checks, not a dataset-wide accuracy
estimate; unresolved defects remain flagged.

Prepare and install a local repair plan from `tools/dataset_pipeline`:

```powershell
.\.venv\Scripts\python.exe -m bcdata.repair_a2d2_conversions --prepare --regression-report <complete-evaluation.json>
.\.venv\Scripts\python.exe -m bcdata.repair_a2d2_conversions --apply --plan-file <sealed-plan.json>
```

Preparation writes immutable draft files without changing active labels.
Installation rechecks source, geometry, model comparison, code, configuration,
review history and live claims under one transaction. It skips any newly
reviewed image, archives previous conversion/QC bindings, and retains original
file bytes. A clean repaired group receives up to three new human samples.
Unchanged blocked groups cannot receive replacement samples just to bypass a
failed audit. Pending repairs stay out of the training export until their new
sample audit passes. Previously frozen training datasets remain unchanged.

## Remaining front-center inventory

`Start-Audi-Import.cmd` starts or resumes the authorized remaining import.
Its pinned plan selects all 27,448 front-center pairs outside the initial
4,000-item plan, including closely spaced frames before duplicate filtering.
The complete paired inventory contains 31,448 frames across 23 drives. All
existing whole-drive split assignments and source metadata pins are preserved.

The worker uses the installed `.gpu-venv` for its dependencies while explicitly
hiding CUDA and selecting the CPU comparison model. UFLDv2 training keeps the
GPU and its existing frozen dataset. Seven cumulative selections of 4,000,
8,000, 12,000, 16,000, 20,000, 24,000 and 27,448 allow progress to resume without
repeating completed preparation. Each wave audits only newly prepared images;
existing curated images and human records remain protected.

Each wave creates fresh risk/split/light/lane-count groups with three actual
human samples when available. Network chunks of 64 do not create audit groups.
Duplicates are filtered before model preparation. Flagged conversions remain
outside training, and clean unsampled conversions wait for their actual sample
approvals. New wave data can enter a later eligible export and training snapshot.

Overall progress is in
`BrakeCheck_Dataset/reports/a2d2_remaining-20261018/progress.json`; the current
wave's download, preparation and publication counts are in
`BrakeCheck_Dataset/reports/a2d2_import.json`. The worker checks raw/output
working space before each wave and waits automatically when needed. Interrupted
or partial waves retain their exact selection when restarted with the launcher.
The plan, checksums and direct CLI are documented in that report folder's
`README.md`.

The phone reviewer opens on **Source audit sample**. Its headline count is the
number of actual samples left, rather than the full flagged-conversion backlog.
Flagged conversions are listed separately and remain excluded from training;
opening them for individual correction is optional. Zero samples means the
available sample audit is complete, while conversion repairs and later import
batches can remain unfinished. The **All review candidates** and **Flagged
conversions** filters retain access to that work. Stale or unclassified cases
remain separately counted as individual review tasks. This presentation does
not approve labels, weaken QC, or release blocked groups.
