# Fresh College repair audits

Open `/source-audits?source=college_held_repairs`, or select **College held photos · fresh repair audits** in Source audits. The Studio datasets page links to this queue.

**To check** contains candidates that passed the observed-geometry checks. Inspect the original photograph and candidate paint, including missing boundaries, slots and visible gaps. Each group contains at most 100 photos, with beginning/middle/end samples, or every photo for groups smaller than three.

**Needs repair** contains representative problem cases. Their specific blockers remain visible and approval is disabled. Exclude or skip these samples, or save a correction to document the issue. A correction of one sample does not repair its neighbors. An exclusion blocks its group; a skip retains the hold.

Use **Show original lines** to compare the untouched imported geometry with the candidate. Return to the candidate view before approving. Existing source approvals are kept separately and are never reset by these fresh audits.

The queue combines source-trace rechecks and explicitly identified model proposals. The selected existing adapted comma checkpoint is pinned in the model receipt, along with its training/data provenance. SAM3 pilot results are retained as diagnostics. Neither proposal generation nor these endpoints train a model or promote model output to original human label truth. The training hold remains active. Passing a sample group records candidate quality for later explicit integration.

The isolated collection is `BrakeCheck_Dataset/experiments/college_held_repairs-20261022`. Its `manifest.jsonl`, `audits.json` and `import.json` bind candidate geometry, original source files, model evidence and group membership with SHA256 hashes. Fresh decisions and immutable decision events live in its own `review.sqlite`. Editing a sample's physical geometry holds that cohort for another repair. Changing source, candidate or evidence bytes invalidates the affected audit.

`adapted-full/proposal-index.json` records every missing-label proposal and disposition. The whole-batch verification receipt is in `BrakeCheck_Dataset/reports/college_held_repairs-20261022/proposal-verification.json`. Ordinary source diagnostics are in `reports/college-repair-20261022/`. The final `staging-receipt.json` records the published photo, group and sample counts.

The reproducible preparation entry point is `bcdata.college_repair_prepare` in the review environment. It requires a completed, verified proposal index, exact ordinary diagnostics, complete remaining-held membership and unchanged source bindings. It refuses to overwrite a published batch or its reviews. A later revision must preserve the existing audit batch and its decisions.

The user-authorized continuous middle boundaries for already approved audits are saved separately in `middle_boundary_annotations.json` and its immutable `boundary_annotations` files. They retain observed paint sections and mark gap interpolation explicitly. Original votes and candidates stay unchanged; vehicle occlusions remain breaks. Derived views require the original approval proof to remain current. These connections do not promote the batch into training. Preserve this revision when resuming; do not rerun it over the existing index.

To draw a replacement, select its slot and remove the old line, then click the intended endpoints. Nearby different lines cannot capture drawing clicks. Dragging an existing point works on the selected slot's handle. A replacement of a segmented boundary saves as an explicit continuous section. An older open two-point draft can retry Save without first refreshing; save or cancel the draft before refreshing to load updated editor behavior.

On 2026-10-22 the user explicitly authorized integrating the already cleared repairs into the converted collection and starting training. `converted_merge` supports this through the opt-in `--source college_held_repairs` adapter, alongside the existing real sources. Ordinary audit endpoints remain separate and do not automatically promote future approvals. The frozen integration includes exact approval proofs and candidate/model/native/effective geometry evidence, including saved middle-line connections. The collection pointer and live training reports describe the published selection; remaining pending/problem photos stay out. See `reports/college-converted-training-20261022.md` for this authorized run and its receipts.
