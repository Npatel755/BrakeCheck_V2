# Dropped-image recovery, October 19, 2026

The former duplicate filter compared 64-bit perceptual hashes and could discard
different roads with similar compositions. Rechecking 9,502 dropped images
identified 9,463 recoverable images and 39 verified nearby pixel duplicates.
Fourteen separate hard image-quality rejections were preserved.

Recovery restored all 9,463 images behind source-conversion checks. It created
150 real audit samples in 59 groups, with three samples where group size allows.
Other source conversions stay held until their audit samples are approved
unchanged. Existing decisions and original image/mask files were preserved.
The 137 missing comma10k source annotation records were reattached only after
their local masks matched Git blob hashes in their original pinned import plans.
Previously unassigned recording splits follow existing session assignments or
the original expansion's deterministic train/validation rule. No existing split
was moved.

Audi import, comma10k expansion, and general curation now use perceptual hashes
only to find candidates. A non-identical candidate must come from the same
source, recording and image dimensions, within 30 frame indices, and pass strong
full-image and road-region pixel comparisons before duplicate exclusion. Exact
content-hash duplicates remain detectable across sources.

`reports/dropped_recovery-20261019` contains the frozen original selection,
per-image duplicate evidence, source-conversion results, mask reattachment
evidence, and validation. `dropped_image_recovery_history` preserves the prior
image records, exclusion reasons and new conversion checks. Production mutations
were preceded by SQLite and human-label backups. Recovered source conversions
remain separate from explicit human approvals.

A supplementary read-only check measured native source runs on 6,438 softer
flagged conversions. Thirty passed the strict representation check, but only
four past human-reviewed controls met the same evidence rule. That is below the
ten-control publication requirement, so the new audit policy was not applied.
Its report and publication decision are in `reports/native_run_audits-20261019`.
Geometry, original QC warnings and existing failed audit groups remain intact.

Training is held by `reports/training_hold.json` at the user's request. The
earlier frozen baseline preparation is retained as historical evidence and must
not be reused for the expanded data. A new snapshot is required after audits.

Validation: 38 focused tests passed. Real browser checks passed for desktop and
phone, confirming that loading the next image does not wait for statistics.
SQLite quick-check passed, and no recording session crosses split boundaries.
