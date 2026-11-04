# ZOD clearance and training snapshots

The Studio **ZOD repaired audits** queue separates individual human approvals,
source-audit clearance, explicit repair photos and source conversions awaiting
audit evidence. An empty queue is not a fully cleared dataset. Review later
and unresolved repair cohorts remain visible separately.

Whole-photo confirmation of unchanged source XY labels is valid sample evidence.
Changing the labels is evidence that the source converter needs repair; the
saved human label is usable individually, while its source cohort remains held.
Missing/invalid native annotations cannot be cleared for peers by confirming a
single empty photo. Skips and stale approvals cannot release cohorts.

The explicit cause-separated migration preserves every previous sample, source
geometry and human event. It archives the original plan in
`zod_audit_*_before_cause_v2`, freezes partitions by machine repair cause, and
selects up to three deterministic samples per new partition (all for smaller
groups). It does not partition by which votes passed. Historical true human
failures remain holds. Successful source-supported boundary consolidation moves
to fresh sample gates after structural checks, rather than remaining permanently
flagged. Findings remain in history; an old gate cannot erase a later diagnosis.

Run the migration once (repeating it is idempotent):

```powershell
cd tools/dataset_pipeline
.venv/Scripts/python.exe -m bcdata.zod_clearance --data-root ../../BrakeCheck_Dataset
```

After review, double-click `Prepare-ZOD-Training.cmd` in the project root. This
freezes current eligible labels into a new, isolated UFLDv2 snapshot. The current
commercial-only converted run is the immutable base; its train/validation labels
and reserved test identities remain unchanged. The ZOD source stays explicitly
in the `share_alike` pool with its pinned source notice. The whole ZOD collection
is assigned to one conservative train-only block because recording IDs are not
available. No ZOD evaluation split is invented. Combining it with existing ZOD
evaluation is refused.

Preparation checks source bytes, frozen approval fingerprints, sample evidence,
geometry, actual export anchors, masks/cache/lists, and exact/perceptual overlap
with protected evaluation images. Nontrainable or suspected duplicate photos
are reported separately; saved reviews are preserved. Immutable base assets may
share storage, while new metadata and ZOD targets are separate files. Future
training/resume verifies the complete frozen byte manifest.

Status: `BrakeCheck_Dataset/reports/zod_training_preparation.json`. Preparation
never starts or resumes GPU training. The normal commercial-only loader rejects
the mixed pool. The explicit experimental loader supports `ufld_zod_cleared_v1`:

```powershell
.gpu-venv/Scripts/python.exe -m bcdata.ufld_baseline --prepared-run <prepared-run> --experimental-data --warm-start <trusted-local-best.pt>
```

Training selection uses the unchanged base validation cohort. The shared GPU
lock, immutable parent checkpoint, reserved test policy and no-auto-promotion
behavior still apply. A prepared subset is not a claim that all 100,000 ZOD
images are cleared.
