# Conversion backlog repair and diagnosis

The 24,139 pending conversion flags were not 24,139 confirmed bad images.
They mixed missing source fragments, ambiguous lane identities, curve/extent
problems, source/model disagreements and coverage heuristics. Past human
reviews showed that simply clearing common flags would also retain real
errors. Source masks are paint segmentation, not lane-instance ground truth.

## Installed pass — 19 October 2026

- Evaluated 22,045 eligible pending conversions and 2,156 existing human-review
  controls. Reviewed/claimed/deferred images and failed audit cohorts were
  protected from bulk replacement.
- Installed 434 native-source repairs: 431 Audi and three comma10k photos.
- Created 16 fresh audit groups, at most 100 photos each, with 36 actual human
  sample tasks. Groups with three or more photos require three samples;
  smaller groups require every photo.
- The qualifying regression set contained 197 actual approved controls. Every
  proposal matched the source-supported human lane identities under the local
  raster comparison, with no regressions. This is a development check, not a
  new UFLDv2 benchmark or a guarantee about unreviewed photos.
- Kept all 434 repairs outside training pending their fresh audits. A corrected
  or rejected sample blocks its group. No human approvals were manufactured.
- 23,705 flags remained immediately after the repair publication. Subsequent
  human reviews change that live number independently.

The new converter uses connected native paint strokes before row tracking,
allows only uniquely anchored refinements and preserves source-supported
existing boundaries. Identity changes require native stroke ordering and
must pass QC and the new audit. Double markings require two actual symmetric
stripes; beyond-four paint requires two contemporaneous source-supported
neighbours. Extrapolated traces cannot supply either exception.

Original source masks, old automatic geometry, human annotations, recording
splits and frozen training datasets are retained. SQLite/human-label backups
and immutable per-image replacement histories precede publication.

## Reviewer workflow

1. Complete **Audit samples** first. The 36 new samples can release their
   repaired cohorts only through actual human sample approvals.
2. **Repair examples** contains 59 representative photos across 20 source /
   failure categories, normally three per category. These diagnose recurring
   failures without opening every flagged photo.
3. Reviewing a repair example changes only that photo. It does **not** approve
   or release other photos in the failure category. Skip remains Review later.
4. Remaining flags stay held. This pass did not solve the whole backlog, and
   completing the repair examples is not equivalent to fixing those labels.

Repair-example selection binds current source and geometry IDs. Changed
geometry automatically leaves that diagnostic queue. Both desktop and phone
support the category, normal approval/correction/exclusion and skipping.

## Evidence and commands

Reports under `BrakeCheck_Dataset/reports/`:

- `native_trace_repairs-20261019-v2/`: frozen inputs, streamed control/backlog
  results, sealed code/config/file hashes, drafts, applied report and preview.
- `native_trace_known_failure_checks/results.json`: all four previously known
  difficult Audi cases remain held under this method.
- `conversion_diagnostics-20261019/`: selected examples and backlog mechanisms.
- `native_negative_discovery.json`: read-only experiment, not promoted. The
  transverse-only subset had insufficient prior controls; no negative labels
  were inferred from that experiment.
- `training_hold.json`: UFLDv2 fitting remains explicitly held.

Run from `tools/dataset_pipeline`, using a new immutable generation directory:

```powershell
.venv/Scripts/python.exe -m bcdata.repair_trace_backlog --generation ../../BrakeCheck_Dataset/reports/NEW-GENERATION --workers 6
.venv/Scripts/python.exe -m bcdata.apply_trace_repairs --generation ../../BrakeCheck_Dataset/reports/NEW-GENERATION
.venv/Scripts/python.exe -m bcdata.conversion_diagnostics --generation ../../BrakeCheck_Dataset/reports/NEW-DIAGNOSTICS --apply
```

The installer rejects changed/incomplete evaluation bytes, unsupported source
control results, current human work and changed label bindings. Repair groups
never release through diagnostic decisions. Do not clear the training hold or
restart fitting merely because preparation has completed.

Validation: 73 focused tests passed. Headless Edge verified desktop and phone
navigation for both ordinary review and Repair examples, including advancing
while statistics requests remain pending. No JavaScript errors occurred.

## Missing-split follow-up

The first recovery publication included 25 curated photos without a split.
The reviewer attempted `null.toUpperCase()` and stopped loading the image.
`bcdata.repair_missing_splits` assigned all 25 using existing/frozen recording
assignments first, then the deterministic recovery-extension rule for unseen
recordings. No existing assignments or human labels changed. Curated photos
with missing splits and recording-session conflicts are now both zero.

The browser renders an unassigned split safely. A review can still be saved
under the derived `unassigned` PC folder, but both human-approved and
sample-validated photos with missing splits are held out of UFLDv2 export.
The export report lists those holds instead of failing the entire export.
Frozen training datasets are unchanged. Evidence:
`reports/missing_split_repair.json` and the dedicated missing-split desktop /
phone browser regression check.

## Follow-up from completed diagnostic reviews — 20 October 2026

All 59 repair examples were reviewed: 41 approved unchanged and 18 corrected.
These deliberately selected examples are development evidence, not an estimate
of accuracy across the backlog. Lane identity and crossing/shape examples
contained recurring real errors. All sampled missing-fragment and teacher-
disagreement cases passed unchanged; their other flags must still be checked.
The decision/file fingerprints and current counts are recorded in
`conversion_diagnostics-20261019/completed-review-analysis.json`.

The next sealed pass adds these source-only checks:

- Ordinary refinements retain source-supported lane slots. Physical paint
  retention alone cannot justify moving a curved right boundary to a left slot.
- Coordinate-order checks compare boundaries at one common measured foreground
  depth when unique source ordering proves it. Comparing different endpoint
  depths could incorrectly mark a valid curved lane pair as a slot conflict.
  This changes reference metadata, not the actual painted-line traces.
- A separate foreground-order reconstruction can replace old slot assignments
  only from unique measured paint ordering and stable paint on both sides of
  the camera centre. Unresolved fragments and missing old supported paint
  still prevent publication. Human geometry is used only for evaluation.
- Native coverage can account for a fully terminating, narrowing paint cap at
  most four rows beyond a supported endpoint. Missing dashes and interior
  gaps cannot use that exception; exported trace extents are unchanged.

Complete native evidence may route teacher-count disagreements to distinct
fresh audits. No source mask or teacher prediction becomes a human approval.
Each source / reconstruction mechanism requires its own
past-review evidence before publication: at least 20 qualifying controls,
including 10 actual unchanged approvals, at least 95% source-supported human
agreement and no regressions on unchanged approvals. Teacher-count disagreement
uses the same source-only algorithm and evidence but requires separate actual
audit samples; it is not mixed with teacher-agreement cohorts. Audit groups retain the
100-photo cap and three actual samples (all photos for groups smaller than
three). Training remains held.

Published pass: `reports/native_trace_repairs-20261020-full-v6/` evaluated
21,550 untouched pending photos and 2,403 past human controls. It installed
140 candidates (138 Audi, two comma10k) in 12 fresh groups with 26 real sample
tasks. The established Audi path matched all 225 qualifying approvals; the
independent ordering path matched all 39 qualifying approvals; comma10k
matched all 38. Controls overlap between algorithms and must not be counted
as distinct people/photos. No unchanged-approval regressions occurred.

All 26 fresh samples were subsequently approved by the user, passing all 12
groups and releasing the 140 images. The validated export at revision 6006
contains 16,336 eligible images: 13,086 train, 1,586 validation, 1,664 held-out
test. There are still 23,504 flagged conversions. The pass did not solve those
remaining cases. No automatic training restart occurred.

Evidence includes immutable inputs/results, code snapshots, backup and
replacement histories, `validation.json` and `completed-audit-summary.json`.
The final diagnostic/known-failure check is in
`reports/native_trace_diagnostics-20261020-final-v6/`: all four known difficult
cases remain held and the single-boundary slot-switch regression is resolved.
44 focused tests passed before publication; 17 audit/publication tests passed
after adding correct native-trace policy attribution to audit evidence.
