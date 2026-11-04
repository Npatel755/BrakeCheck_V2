# BrakeCheck dataset review on your phone

Lead-car labeling (24 October 2026): open **Lead cars** in Studio or
`http://127.0.0.1:8765/lead-cars`. The first batch has 500 vehicle-candidate
photos for your boxes, separate from lane reviews. See
[`LEAD_CARS.md`](tools/dataset_pipeline/LEAD_CARS.md).

ZOD clearance update (24 October 2026): see
[`ZOD_CLEARANCE.md`](tools/dataset_pipeline/ZOD_CLEARANCE.md). The ZOD queue now
separates individual approvals, cleared source labels, explicit repair cases
and pending source audits. Unchanged whole-photo confirmations no longer create
false converter failures. Use `Prepare-ZOD-Training.cmd` after review to prepare
an isolated training-only snapshot with explicit share-alike provenance; this
does not launch training or change the existing commercial-only collection.

Double-click **Start-Dataset-Review.cmd** in this folder. It starts the local PC server and opens the desktop reviewer. Click **Pair phone**, connect the phone to the same Wi-Fi, and scan the QR. Enter a reviewer name. The pairing code is also shown for manual entry.

On iPhone, open the page in Safari and use **Share â†’ Add to Home Screen** for a home-screen shortcut. This is a locally hosted web app, not a separately signed iOS binary. Keep the PC awake and the server running. There is no public deployment or cloud image upload. The shortcut requires a connection to the PC; offline reviews are not claimed saved.

The current desktop URL is `http://127.0.0.1:8765`. The host page discovers the current Wi-Fi address for the phone. At initial setup the Wi-Fi address was `http://10.90.33.157:8765`; it can change. `localhost` on the phone means the phone itself, so use the QR or the PC's Wi-Fi address.

If a phone cannot reach the page, use the same private Wi-Fi or a hotspot shared by the two devices. Campus eduroam can isolate clients. Windows already has inbound Python rules for public/private networks; no firewall changes were made. If Windows prompts for this Python server's network access, allow it on the network you intend to use.

## The three decisions

- **Human approve** saves the displayed painted-lane proposal unchanged as human-reviewed geometry. It does not turn unreviewed road edges into negative examples.
- **Exclude photo** asks for confirmation, then excludes the photo from both training datasets and places it in the rejected category. Use this for unusable photos, not simply because no paint is visible.
- **Edit labels** opens the editor for painted lines and separate road edges. **Save correction** puts the corrected result in human approved. A photo with no visible paint is a valid painted-lane negative; it can still have visible road edges.

## What to draw

The editor separates **Painted lines** and **Road edges**. A road edge must never occupy one of the four painted-lane slots.

| Label | Trace | Example |
|---|---|---|
| Painted lines | Center of visible painted markings | Yellow centerline or white dashed line |
| Left/right road edge | Visible physical boundary of the roadway on that side | Pavement meeting grass, a curb, or a barrier |

Painted slots are `1=outer left`, `2=ego left`, `3=ego right`, `4=outer right`. Ego means the camera car's lane. Keep slot 3 absent when the right side has no painted marking, even if a curb is visible. Follow a dashed marking as one boundary through its visible sequence of dashes; represent a double marking as one boundary centered between its two stripes. Do not add crosswalks, arrows or stop lines.

For a curb, trace its foot where the road surface meets it, not its upper edge. For an unmarked pavement edge, trace the visible pavement-to-grass, gravel or other surface transition. Select the edge kind consistently: **Curb**, **Pavement edge**, **Shoulder**, **Barrier**, or **Other**. A shoulder edge is the visible outer boundary of the roadway/shoulder surface, not an arbitrary line inside a continuous paved shoulder. A barrier edge is its visible contact boundary with the road. Do not trace parked cars, shadows, internal pavement patches, or seams as road edges.

Each road side has an explicit status:

Opening an unreviewed side in **Road edges** starts its draft at **Visible â€” trace it**, ready for points. Draw at least two points or choose another visibility before saving. Switching tabs preserves explicit visibility choices. Saved and legacy labels are unchanged until you save a valid review.

- **Visible:** draw at least two points along the observed edge and select its kind.
- **No boundary here (absent):** you inspected that side and the image supports no road edge there, such as an open junction with no continuing boundary.
- **Unclear / hidden (unknown):** darkness, occlusion, cropping or ambiguity prevents a reliable decision. Do not guess an edge or treat this as a negative.
- **Not reviewed:** no road-edge decision has been made. All older labels start here; older lane approvals remain usable for painted lanes.

Both label types should follow only the observed extent. Add points to follow curves and stop before hidden or uncertain sections. The shaded regions mark the crop used by the painted-lane UFLD export; the separate road-edge export keeps coordinates in the original full image.

For example, a road with a yellow centerline and an unpainted right curb gets **ego left, slot 2**, plus **right road edge: Visible / Curb**. Painted ego right stays absent. A road with no paint can have zero painted lines and two visible road edges.

## Editing and recovery

Choose **Painted lines** or **Road edges**, then select a line/edge and drag its points, or add points along a new boundary. Finish the boundary before starting another. **Undo last point** removes the newest point without deleting the whole line. **Undo edit** and **Redo** recover draft changes before saving. These controls affect the open draft; **Undo last decision** below the image reverses a decision that was already saved on the PC.

Opening the Road edges tab does not review either side. Clicking an unreviewed left/right side defaults only that side to **Visible â€” trace it**; explicit visibility choices remain intact. Using a model layer hides its corresponding comparison overlay, while preserving the editable labels and the other model layer. Toggle **Model** off/on to compare again.

**Save correction** automatically repairs finite out-of-image coordinates by moving them onto the image border and merging overlapping image rows. The manual **Auto-fix points** button remains available, and draft fixes support Undo/Redo. A collapsed or malformed boundary that cannot be repaired without inventing points still requires manual correction. No clipping invents missing lanes or edges.

Confirmation dialogs have a **Don't show this confirmation again on this device** checkbox. Remembering an accepted action skips that particular confirmation in this browser; cancelling never records acceptance. Notifications also have **Don't show again**. Hidden error notifications still appear inline beside the save controls and never bypass validation. Restore all these preferences with **Show hidden messages again** in the quick guide.

Save explicitly after reviewing. Selecting **Unknown** is preferable to inventing an edge through an obstacle. **No painted lines** clears only the painted-line draft and preserves road edges. Use it when no paint is visible, then save rather than excluding an otherwise usable photo.

**Source paint** shows the existing comma10k reference mask. **Model** shows separate neural candidates; the selector identifies their producer, whether inference was independent or source guided, and the number of proposed painted lines and road edges. Empty results include an explanation where available. An empty SAM 2 result can mean there was no source paint to refine, so no model inference ran for that photo.

- **Use model lanes for correction** replaces the painted-line draft and preserves road edges.
- **Use model road edges** replaces the two road-edge drafts and preserves painted lines. Confirm before replacing existing edge work, then check each side's visibility, points and kind.

Both actions create editable drafts. A model result becomes human reviewed only after you inspect it and tap **Save correction**. A model road contour approximates the edge of its predicted road region; it does not establish that a line follows a real curb. Correct its location and kind using the photo. Missing or unreliable model edges stay **Unknown**, never automatically **Absent**.

**Skip for now** makes no decision. Filters let you revisit approved, corrected, or rejected images. **Activity** opens recent decisions. **Undo last decision** restores that reviewer's previous state, unless a newer decision has already changed that image. Multiple phones receive different active queue assignments; revision checks also prevent stale saves.

The default queue prioritizes the test split, then validation, then training; difficult cases rank first inside each split. These are review assignments, not permission to train on the test set.

## What has been prepared

The original ZIPs are unchanged. The working copy is in `BrakeCheck_Dataset/` and is excluded from Git.

- 2,000 source photos are restored and SHA256 checked against the supplied registry. Sixty-three were absent from the exported photo folders and were restored from the original source.
- All 2,000 original segmentation masks are restored and verified against the Git blob hash prefixes recorded in the registry. The three-class exported masks were not substituted for the full original annotations.
- The existing quality/curation decisions leave **1,973 curated images**; 25 dropped duplicates/curation cases and 2 source-QC rejects are listed separately.
- Source annotation, fusion, lane geometry and QC were run on the 1,973 curated images. There are 207 test, 180 validation and 1,586 training images before review filtering.
- Recording-session parsing was repaired for prefixes such as `r055_`. The actual split audit found no session crossing split boundaries, so the existing split memberships were retained.
- Source-based lane proposals are automatic until reviewed. Existing paint masks being human-labeled does not make the automatically fitted lane geometry human approved.

The imported comma_segnet teacher geometry can be compared. Its dense probability maps were not packaged and have not been reconstructed by pretending the source mask was a model prediction. The old evaluation report is historical evidence, not a newly rerun model score.

## Where decisions and exports go

| Artifact | Location under `BrakeCheck_Dataset/` |
|---|---|
| Working metadata and decisions | `metadata.sqlite` |
| Immutable, versioned human geometry | `annotations/reviewed/<image_id>/<event_id>.json` |
| Human approved photos and JSON | `reviewed/human_approved/<split>/` |
| Human rejected photos and JSON | `reviewed/human_rejected/<split>/` |
| Original imported archive files | `imported/BrakeCheck_Dataset/` |
| Reviewed-only UFLD dataset | `exports/ufld_v2/BrakeCheck_Reviewed/` |
| Separate reviewed road-edge dataset | `exports/road_edges/BrakeCheck_Reviewed/` |
| Export status | `reports/reviewed_export.json` |
| Preparation audit | `reports/preparation.json` |
| Model availability and progress | `reports/model_status.json` |
| Model license snapshots | `licenses/` |
| Backups | `backups/<timestamp>/` |
| Server logs | `logs/review-server.*.log` |

The category folders use hard links when available; they do not move or edit source photos. SQLite is authoritative. Review decisions commit transactionally; repeated network retries use request IDs to avoid duplicate decisions. Human geometry is versioned rather than overwritten. New model-adoption saves check the displayed candidate's exact version, so regeneration while you edit requires reloading that candidate. Painted-line and road-edge model provenance are retained separately, and license dependencies accumulate when both are used. The reviewer offers a consistent database/label backup.

The reviewed UFLD export refreshes automatically after decisions. It contains **only human approved/corrected, commercial-pool images**. Pending and rejected images are excluded. It preserves train/validation/test membership, creates 1640Ã—590 image canvases, CULane `.lines.txt` annotations, lane-index masks, lists, training cache, a manifest, configuration and notices. Valid no-lane negatives have zero existence flags. Corrections update the actual exported polylines and masks.

Road edges are exported separately as original-image coordinates in JSONL, with images and a manifest. They are not inserted into UFLD's four lane slots or painted-lane masks. Records require an explicit decision for both road sides; **Unknown** remains recorded with its training mask disabled. **Not reviewed** is not an absent edge and does not become a road-edge training negative. The edge export requires a dedicated training adapter/model setup before training; its format does not claim compatibility with stock UFLDv2.

`READY.json` identifies the published snapshot, manifest checksum, review completion and whether all splits are nonempty. An early export is a **partial review**, not a completed dataset. Compare its revision against the working registry before consuming it. The export is validated across all three splits; the original train/cache validator also runs. The GPU DALI training loader has not been claimed verified by these CPU format checks.

No UFLDv2 training, DINOv3 decoder training, phone model replacement, IPA build or deployment is triggered by reviewing images.

### Authorized DINOv3 decoder experiment

The user separately authorized starting decoder training. `Start-DINOv3-Training.cmd` launches a hidden worker for the pinned official DINOv3 ViT-7B backbone. It verifies resumable model downloads, waits for other large-model jobs and enough free VRAM, then measures actual GPU fit using three optimizer steps and four training/four validation source-labelled photos. After that succeeds, it starts a 20-epoch decoder run, keeping the pretrained backbone frozen in BF16. No smaller-model substitution is automatic.

This first decoder learns the original human pixel masks, not the reviewer's corrected polylines. Only curated commercial training/validation images eligible when the cohort is frozen are used; dropped and rejected images are excluded. A cohort manifest records the experiment and recording sessions must not overlap. Test images remain reserved. Checkpoints include revision, precision, license and source-cohort provenance. The best validation checkpoint is saved separately and requires comparison before being offered as a reviewer candidate. It does not replace human labels or current SAM proposals.

Progress and errors are in `reports/dinov3_training.json`; the current transfer is in `reports/dinov3_download.json`. The worker waits if memory is occupied by other applications. UFLDv2 training remains deferred. A download or CPU stand-in test is not a completed real-7B fit check or a trained decoder.

## Large models on the RTX 4090

The annotation profile is `tools/dataset_pipeline/config/rtx4090.yaml`. The GPU environment is separate from the review server. CUDA PyTorch `2.11.0+cu128` was installed and verified to see the RTX 4090 (23.99 GiB, BF16 supported). The original global Python installation was retained.

| Model | Purpose | License and access |
|---|---|---|
| SAM 3, pinned official Meta checkpoint | Independent image/text paint and road candidates, plus vehicle masks to avoid tracing through vehicles | [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE): commercial use is permitted subject to its conditions. Account access is now confirmed; download/inference progress appears in the model panel. |
| SAM 2.1 Hiera Large, official large checkpoint | Refine source-paint candidates, one component at a time | [Apache-2.0 covers code and checkpoints](https://github.com/facebookresearch/sam2#license). This result is source-guided and must not be presented as an independent accuracy test. |
| DINOv3 ViT-7B/16 | Reserved feature backbone for a future trained lane decoder | [DINOv3 License](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md), subject to its conditions and gated access. Not enabled as a lane model; there is no trained lane decoder yet. Actual 24-GB fit must be benchmarked before enabling. |

SAM 3 reads the photo and text prompts for painted markings, road surface and vehicles. It receives no source paint mask, human points or fitted source geometry. It encodes each photo once and reuses those image features for its prompt forwards. Its predicted vehicle masks help suppress road-edge proposals through occlusions; this does not create a separate human-reviewed vehicle dataset. Road-edge contours remain proposals that require inspection.

The current image candidate path uses the pinned official [SAM 3 model](https://huggingface.co/facebook/sam3). Larger parameter counts are not a measured lane-accuracy ranking. Tune prompts and select models on validation; reserve test for final evaluation after choices are frozen. Check the current model report for successful loading, inference, errors and measured GPU memory; account access alone does not establish a completed run.

`Setup-Large-Models.cmd` sets up the isolated CUDA environment and fetches official license snapshots. SAM 3 account access is confirmed. If credentials need renewal, `Sign-In-HuggingFace.cmd` signs in privately with a Hugging Face read token; keep the token out of chat and project files. DINOv3 remains reserved for later decoder work; check its separate [model access](https://huggingface.co/facebook/dinov3-vit7b16-pretrain-lvd1689m) when that work is requested.

`Run-Large-Models.cmd` resumes candidate generation, serially loading SAM 3 and SAM 2.1 Large so they do not compete for VRAM. It reuses complete versioned outputs and generates comparison candidates for all curated photos, including previously reviewed ones. Human labels, review decisions and approved exports remain unchanged until a reviewer explicitly adopts and saves a candidate. A denied model reports waiting for access; it is never silently replaced with a tiny model. Inference failures are reported, not marked complete. The progress panel shows the actual state. There is no training in this command.

Progress counts distinguish completed candidate files from actual model execution. **Done** includes valid resumed outputs, including empty ones. The report separately records image-encoder and prompt/model forwards, nonempty/empty painted and edge results, and the number of existing human reviews preserved. Historical outputs without execution counters retain an unknown count. These counters describe processing and output content; they are not accuracy scores.

Licenses and exact Hugging Face revisions are recorded per model. If you adopt a model candidate, its license IDs follow the human geometry into the export; downloaded model-license texts are bundled in the export. [comma10k's MIT notice](https://github.com/commaai/comma10k/blob/master/LICENSE) is retained for the source images. UFLDv2's code license does not establish that an arbitrary downloaded lane checkpoint is suitable for commercial use; checkpoint provenance must be verified separately before later training.

## Commands and checks

Run from the project root in PowerShell:

```powershell
# Starts the PC server and opens the desktop reviewer
.\Start-Dataset-Review.cmd

# Review state and explicit reviewed-only export
tools\dataset_pipeline\.venv\Scripts\python.exe tools\dataset_pipeline\review.py status
tools\dataset_pipeline\.venv\Scripts\python.exe tools\dataset_pipeline\review.py export
tools\dataset_pipeline\.venv\Scripts\python.exe tools\dataset_pipeline\review.py backup

# Rebuild/resume preparation; exact source files are reused after verification
tools\dataset_pipeline\.venv\Scripts\python.exe tools\dataset_pipeline\review.py prepare

# Focused pipeline, geometry and review regression tests (disposable data)
tools\dataset_pipeline\.venv\Scripts\python.exe -m pytest tools\dataset_pipeline\tests -q

# Real Chromium desktop/mobile-emulation check; needs Playwright and Edge/Chrome
tools\dataset_pipeline\.venv\Scripts\python.exe tools\dataset_pipeline\tools\check_review_ui.py

.\Stop-Dataset-Review.cmd
```

The browser check uses a separate disposable dataset and tests phone pairing over the PC's LAN address, point dragging, correction persistence, rejection, undo, approval, export and JavaScript errors. It never marks real dataset images as reviewed. Browser mobile emulation is not a claim of testing on a physical iPhone.
# Windshield-view expansion (17 October 2026)

The review image now has two separate views: **source paint and model predictions on the left**, and **painted-line/road-edge labels on the right**. Only the right view edits annotations. The reference view cannot add or move nodes. Source/model visibility toggles apply only to the reference; label visibility applies only to the editable view. The panels share zoom and scroll, stack on phones, and offer **Hide reference** for more editing room. Saving or adopting a model still requires the existing explicit action; rearranging the display changes no labels.

The `comma10k-remaining-20261017` import plan contains **6,520 additional eligible, previously unseen recording sessions**, about **4.689 GB** of pinned MIT photos/masks. This is the eligible original `imgs/` subset, not an assertion that every file in the full repository has suitable road labels. Images already present, repeated recordings, unavailable/unreviewed source masks, interior views, duplicates and hard QC failures are excluded. The worker prepares source-derived lane geometry and independent CPU fresh-model candidates, then publishes small chunks into the normal pending queue. It never automatically approves images or changes the completed baseline's snapshot. Progress: `BrakeCheck_Dataset/reports/dashcam_expansion.json`. Resume the same batch with the GPU environment's Python from the pipeline directory: `python -m bcdata.expand_dashcam --limit 10000 --batch comma10k-remaining-20261017 --workers 12 --chunk-size 32 --paint-model`.

A deterministic 24-photo train/validation conversion audit and six-photo contact sheet are in `BrakeCheck_Dataset/reports/conversion_audit_20261017/`. Source paint generally follows the fitted traces, but double-line slot interpretation and short/crossing markings still need review. Two sampled source masks had paint with no slotted proposal; that can be valid for short/transverse paint and is not automatically an annotation error. Pixel-consistency diagnostics are not annotation-accuracy scores.

## Provisional UFLDv2 baseline

The user authorized baseline training on the current accepted data. `Start-UFLD-Baseline.cmd` starts 20 epochs; the live report is `BrakeCheck_Dataset/reports/ufld_training.json`, also displayed in the reviewer's Pipeline panel. The frozen run dataset contains 1,752 train and 225 validation images from export revision 2592. Test pixels/labels are not loaded. Later reviews do not change this snapshot. Architecture: upstream UFLDv2 ResNet34, ImageNet1K V1 backbone initialization, randomly initialized lane heads, BF16 GPU forward, effective batch 32 via batches of 8 and four-step gradient accumulation. Lane checkpoints trained on CULane/TuSimple are not used.

The native Windows adapter preserves slot identities, negative images and unannotated gaps; it does not use DALI or its CUDA interpolation extension. No bottom extrapolation is added beyond approved spans. Validation uses a Python raster lane F1 at 30-pixel line width and IoU 0.5, not the official CULane executable. The run saves its own source, pinned upstream revision/patch/license, dataset hashes, model initialization hash, loss history and best/last checkpoints. Select checkpoints on validation; leave test reserved. This is a provisional baseline, not a released phone model. Resume a saved run with `tools/dataset_pipeline/Start-UFLD-Baseline.ps1 -ResumeRun <run-directory> -Epochs 20`; it recovers the last completed epoch and optimizer state, with fresh random augmentation order. Snapshot data and the reviewer stay separate.

The completed **DINOv3 7B trained decoder** is available as an optional model comparison, including batch review. Its predictions are generated separately; they never replace source proposals or saved human labels unless explicitly adopted and saved. Select it in the Model dropdown when available. The model is selected on the original validation cohort, not independently tested on phone recordings. On 161 original validation photos with lane paint, a common native-resolution threshold-0.5 mask comparison yielded fresh comma 62.93%, DINO 61.31%, SAM3 54.27% mean lane IoU. These differ from the training report's resized argmax metric (DINO best 66.05%). Fresh comma may have trained on these images. None of these figures measures editable polyline accuracy or phone deployment performance. Full comparison provenance: `BrakeCheck_Dataset/reports/dinov3_comparison.json`. Dataset-wide DINO generation progress: `reports/dinov3_proposals.json`.

The `windshield-proxy-20261017` batch adds previously unseen road-facing comma10k recordings as a proxy for a phone mounted behind a windshield. Exact phone hardware is **not verified**; these are not advertised as iPhone recordings. Original MIT images and community segmentation masks are fetched from a pinned repository revision with Git blob checksum verification. The import excludes existing recording sessions, exact duplicates and perceptual near duplicates, and includes only one frame per new recording. It keeps the existing test set and all saved human decisions intact. New sessions receive deterministic train/validation assignments and enter the ordinary queue with source-derived painted-line proposals, requiring human approval. Road-edge status stays unreviewed. The batch provenance and fetched license are stored under `BrakeCheck_Dataset/sources/windshield-proxy-20261017/`; progress is in `reports/dashcam_expansion.json`.

Run `python -m bcdata.expand_dashcam --limit 500` from the pipeline directory with its review environment to recover this same pinned batch. This does not launch UFLDv2 training or alter a running DINOv3 training cohort. Fresh CPU road-marking candidates can be generated with `Start-Paint-Proposals.ps1` after the batch finishes.


## Source conversion audit policy (2026-10-17)
Imported comma10k human source masks no longer make every conversion a manual task. Automated QC and disagreement checks flag suspicious conversions; a deterministic 64-image sample spans split, lighting, and lane-count groups. Unflagged, unsampled conversions remain held outside training until all actual human sample approvals in their group pass. Correcting or rejecting a sample blocks that group for conversion repair. Undoing sample approval revokes eligibility.

Source-validated exports carry a distinct label_origin and fingerprinted sample evidence. They are never recorded as human-reviewed, and original human decisions, immutable annotations, source masks, splits, and the frozen baseline remain preserved. The reviewer now opens on Source audit sample and shows actual audit samples as its headline count. Flagged conversions have a separate visible backlog and remain accessible through All review candidates or Flagged conversions for optional manual correction; they stay outside training until fixed or individually reviewed. Dedicated filters show held and source-validated images. This policy supersedes the older requirement for individual human review of every imported source conversion. Road-edge exports still require their own human reviews.

## Source converter repair and risk cohorts (2026-10-17)
The source-mask converter now joins strongly supported continuations while preserving observed extents and existing lane identities. Short or ambiguous fragments are comparison proposals under “Source paint · recovered fragments”; they do not automatically become painted-line labels. Explicit adoption creates a human correction with source-mask and geometry fingerprints, without adding a model-weight dependency.

Blocked source conversions can be reprocessed with `python -m bcdata.repair_source_conversions`. It writes immutable drafts and a plan. Apply a reviewed plan with `python -m bcdata.repair_source_conversions --apply --plan-file <plan>`. Installation skips images with human review history, active reviewers, or changed fingerprints; archives earlier checks/QC; and creates fresh risk cohorts. New samples are required before unsampled conversions become source-validated. No human approvals are written by this process. Historical blocked groups stay in history and leave active warnings when their pending checks have migrated.

For future comma10k source imports, the geometry stage uses this converter and flags unresolved fragment warnings. Audit cohorts separate unchanged conversions, joined extents, and other changes, with lighting/lane-count diversity within each cohort. Image-suitability rejection remains an exclusion of that image; peers are audited separately, never released automatically.

Future audit preference: default to three distinct images per group, with lighting/lane-count variety when available. Groups containing only one or two images use every available image. The per-group minimum takes priority over the overall sample target. Every selected sample must pass before its group is released. Apply this to future imports and repair audits; preserve existing sample assignments and decisions.

Queued benchmarks: a new immutable eligible-data snapshot can be prepared while the current benchmark runs. bcdata.ufld_queue waits for verified successful predecessor completion, checks frozen metadata, and launches one fresh UFLDv2 --prepared-run job under the shared GPU lock. Queue status appears separately in the reviewer. Existing checkpoints/snapshots remain preserved; failed or held audits do not enter a queued snapshot.

## V3 source continuation repair (2026-10-17)
The remaining 2,606-photo hold was traced to two genuine audit failures: a three-row distant paint component could bypass slope checks and bend a foreground lane fit. V3 requires measured local direction across dash gaps and re-extracts only source geometries with long curved joins adjacent to a component shorter than four rows. It also retains actual visible source paint beside the hood instead of clipping every lane at a global hood row. It never extends to a guessed human/model endpoint.

All 2,606 inputs were rechecked, 37 automatic geometries changed, and 2,569 kept their existing lines. After full QC and finer cause/lighting/lane-count grouping, the queue contains 52 audit images across 19 groups (three per group when available), five individually flagged cases, and 2,549 held pending those audits. V3 records remain distinct from human approvals and are not part of the already-frozen benchmark. Optional source-fragment comparison proposals exist for 13 revised cases. Existing human/source files, history and both training manifests were verified unchanged.

Future source imports use this narrow continuation repair when the same risk occurs. Future audit groups now have an explicit maximum of three as well as a default minimum of three, so a total sample target cannot silently expand a group beyond the requested three. Smaller groups use all available images. Old audit assignments remain historical evidence.


To continue UFLDv2 with newly eligible data, refresh the reviewed export and use `tools/dataset_pipeline/Start-UFLD-Baseline.ps1 -WarmStart <trusted-local-checkpoint> -Epochs 20`. This creates a new frozen dataset/run, strictly loads compatible model weights, records the parent checkpoint hash and completed epoch, and resets the optimizer, schedule and validation history. `-ResumeRun` retains its original dataset and does not add later reviews. Warm starts reject previously trained images or recording sessions moving into evaluation, and keep parent test images reserved. Old checkpoints and snapshots remain intact; compare metrics using their recorded validation cohorts.


V4 source repair is installed for the remaining held cohort: 585 rechecked, 19 geometries changed, 12 fresh samples in six cause-specific groups, 530 waiting for those sample approvals, and 43 individually flagged cases. In the reviewer choose Source audit sample. Three samples per group are required where available; smaller groups use all images. Source-only fragment recovery requires overlapping actual source-painted rows and distinct separation before lane-slot promotion; ambiguous continuations, curves and crossings remain flagged. No human labels or current UFLDv2 snapshot changed. Evidence: BrakeCheck_Dataset/reports/source_v4_install-20261017-2353/summary.md. Future trusted comma10k source conversions use V4 after V3/V2; model comparisons and other datasets do not.
