# Conversion backlog recheck

The backlog recheck uses original semantic paint and past human reviews. It does
not train a model or turn a model prediction into a human decision.

`bcdata.source_fragment_continuation` repairs a missing observed continuation of
an existing lane. It requires a unique partner, compatible endpoint tangents,
bounded gap and endpoint error, and compatible Audi solid/dashed classes. It
preserves existing lane slots and the entire previous curve. Double lines,
out-of-contract fragments, forks and unsupported spans remain unresolved.

`bcdata.recheck_flagged_conversions` freezes a selection of the complete eligible
flagged backlog and saved human controls, then checks native source masks,
geometry, completeness, support and topology. A compact exact-colour loader
avoids decoding unrelated semantic classes; tests compare its paint channel
with the established loader for Audi, comma10k and class-map masks.

The production installer, `bcdata.apply_backlog_recheck`, requires at least 100
real approved controls, zero approved-control raster regressions, and unchanged
code, configuration, selections and input fingerprints. It checks current human
decisions, claims, deferrals, geometry/source bindings and quality records again
inside its write transaction. A SQLite and human-label backup precedes changes.
Old geometry and source files remain intact; replacements have immutable paths
and a complete history in `source_backlog_recheck_history`.

A model/source count disagreement is eligible for sampled review only when its
source, lane-count and source-paint pattern has at least 20 prior decisions and
at least 95% were approved unchanged. This observed rate is a triage heuristic,
not a probability guarantee or a training accuracy metric. Other disagreement
patterns remain flagged. A failed prior audit cannot get fresh samples without
an actual geometry fix.

Clean conversions receive new audit groups separated by source, split, lighting,
lane count, repair cause and disagreement risk, with at most 250 members per
group. Three real samples are selected when available, preferring different
recording sessions. Every sample must be approved unchanged to release the
unsampled source conversions. Correcting or rejecting a sample blocks the group.
All samples are created together. Existing reviews, skipped photos, old audit
votes, train/validation/test assignments and frozen training snapshots are kept.

From `tools/dataset_pipeline`, prepare a new generation in a directory that does
not already exist:

```powershell
.venv/Scripts/python.exe -m bcdata.recheck_flagged_conversions --output ../../BrakeCheck_Dataset/reports/flagged_recheck/NEW-GENERATION --workers 6
```

Inspect `report.json`, `progress.json`, `past_review_controls.jsonl`, and
`backlog.jsonl`. Install a passing generation with:

```powershell
.venv/Scripts/python.exe -m bcdata.apply_backlog_recheck --generation ../../BrakeCheck_Dataset/reports/flagged_recheck/NEW-GENERATION
```

`applied.json` records the backup, installed counts, audit sample IDs, skips and
past-review disagreement evidence. Refresh the reviewer to see the new samples.
The recheck is explicit; importing more images does not silently overwrite
existing converted geometry or promote this converter into raw import jobs.
