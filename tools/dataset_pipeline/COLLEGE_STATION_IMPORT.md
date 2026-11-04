The College Station importer retains the official front-camera photographs and
their original SCNN `.lines.txt` annotations in an isolated audit pool. It does
not alter the registry, existing conversions, model proposals, human decisions,
or a training collection.

Official dataset: https://doi.org/10.15787/VTT1/5FGGKD

The publisher's current API identifies the dataset as CC0-1.0. The metadata and
specification PDF are retained with SHA256 hashes. If the publisher rate-limits
the metadata endpoint, an inventory extract from a previously successful API
observation can be pinned instead. `metadata_provenance` then explicitly says
`observed_official_api_inventory_extract`, `metadata_refresh_pending` remains
true, and no exact original API-response hash or unavailable part checksum is
claimed. The observed file IDs, names, sizes, and license are never guessed.
The specification is fetched separately and checked for its observed size and
complete PDF structure. Publisher ZIP
entry CRC32 checks verify each downloaded entry. SHA256 receipts cover each
retained photograph, native label, and converted geometry. Whole ZIP-part MD5
checks are **not** claimed: the raw videos are intentionally never downloaded.

The three annotated archives contain:

| Archive | Paired front-camera frames |
| --- | ---: |
| CollegeStation_dataset | 1,770 |
| RELLIS_3M_Panel_Dataset | 10,668 |
| US290 | 9,733 |
| Total | 22,171 |

The paired image and label payloads occupy about 5.23 GB compressed, versus about
87.86 GB for the publisher's complete video-containing archives. A bounded
4 MB range cache reads the split archives remotely; range responses that silently
fall back to downloading a complete archive are rejected. Neighboring range
blocks add a small amount of overhead. TX21's 4,379 JPEGs and 426 unpaired College
Station JPEGs are excluded because they have no corresponding native lane label.

Each native annotation line is a sequence of `x y` floating-point pairs for one
painted longitudinal line. Coordinates belong to the supplied photograph (some
images are already cropped to 1640 × 590), not a guessed full-camera canvas.
The importer preserves those coordinates and the original files. It derives
four ego-relative slots for review, keeping short, transverse, malformed,
off-image, or ambiguous cases in `hold`. Invalid drawable traces are omitted
while their raw native annotation remains available. Marking pattern is
`unknown` because SCNN files do not distinguish solid/dashed. A blank valid
native file records an empty source annotation; it does not independently prove
that the photograph has no visible paint. Native-empty audit cohorts are kept
separate from native-positive cohorts. Malformed text never becomes a no-lanes
approval.

`physical: true` describes the publisher's intent to annotate a physical
boundary. It is not a claim that BrakeCheck independently verified visible
paint. Every source-only lane explicitly has `visibility: unknown`,
`visible_paint_verified: false`, and
`physical_evidence: publisher_annotation_only_unverified`. Geometry remains
`source_only_awaiting_audit`; these isolated artifacts are never registered or
sent to a painted-line training export by the importer.

Visual QA compared source photographs, native lines, and converted geometry on
all three source archives, including 27 beginning/middle/end US290 previews.
The 3,500-image `US290/west/w_b` material subset demonstrably omits interior
painted dividers or substitutes an outer trace, so every frame in that subset
gets `native_coverage_incomplete` and `split: hold`. Nothing missing is invented
with a model. Exact examples and source receipts are retained in the experiment's
`reports/NATIVE_COVERAGE_REVIEW.md` and `reports/coverage_us290/` folder.

The same College Station physical road shares a split across lighting
conditions. RELLIS is one train-only closed-course physical site, and US290 is
one train-only highway site. This prevents adjacent observations of those sites
from being claimed as independent validation data. Audit cohorts separate source,
lighting, and recording conditions, with at most 250 clean frames per cohort.
Held cases group by physical site and primary QC cause, avoiding small groups
for every lane count and tape segment. Each group selects three deterministic
temporal positions (beginning, middle, and end), or every photograph when fewer
than three exist. No imports become trainable
until the separate audit workflow accepts them.

Run the complete resumable import from the repository root:

```powershell
python tools/dataset_pipeline/bcdata/import_college_station.py
```

A pilot requires a separate output directory:

```powershell
python tools/dataset_pipeline/bcdata/import_college_station.py --limit 12 --output BrakeCheck_Dataset/experiments/college_station_pilot
```

The full output is
`BrakeCheck_Dataset/experiments/college_station_lane_marking/`. `status.json` and
the optional redirected stdout log describe progress. Publisher HTTP 429
responses receive a 60–300 second cooldown (or their supplied `Retry-After`).
Completed artifacts are journaled and SHA256 checked before a resumed run reuses
them. Only the completed source publishes `manifest.jsonl`, `audits.json`, and
`import.json` with `state: awaiting_audit`.

Validation:

```powershell
$env:PYTHONPATH='tools/dataset_pipeline'
python -m pytest tools/dataset_pipeline/tests/test_import_college_station.py -q
```

The focused tests exercise split ZIP boundaries, ZIP64 local headers, entry CRC
validation through `zipfile`, bounded range caching, truncation/rate-limit
handling, fully decoded JPEGs, native coordinate preservation, invalid-label
holds, source negatives, physical-site splits, artifact tampering, deterministic
audit coverage, and interrupted-journal recovery.

The completed 2026-10-21 import contains all **22,171** paired frames, with
**126 audit groups / 358 sample photographs**. Its actual source-local split
counts are **14,699 train candidates / 7,472 held / 0 validation / 0 test**.
All seven College Station physical-road IDs happened to hash into the train
fold; RELLIS and US290 were deliberately kept as train-only physical sites.
This supplemental import therefore does **not** supply its own independent
evaluation set. Any later training must retain the existing independently
frozen validation and test sets. Train candidates still require audit clearance;
the importer itself adds zero trainable images or human approvals.

The held total includes 3,500 US290 `west/w_b` coverage-risk frames. Other QC
occurrences (which can overlap within a photograph) are 3,678 long ego-slot
extrapolations, 511 potentially transverse traces, 272 short native polylines,
45 non-single-valued polylines, 14 center-ambiguous slots, and 7 native
out-of-image cases. Final receipts and independent whole-import verification
are recorded under the experiment's `reports/import-validation.json`.

The audit status separates held photos cleared by existing group audits, those
cleared individually, and those still held. Existing group approvals can clear
ordinary conversion warnings only within their original cohort when every
sample has a current approval, the same warning confirmation, and unchanged
lane geometry. The clearance receipt binds those approvals and source files.
Edited sample lines and whole-photo fixes for missing or malformed labels apply
only to the reviewed photos; they cannot clear neighboring photos. Source
annotations, original held splits, and warning provenance are retained.
Eligibility does not start training; training remains on hold until explicitly
requested.
