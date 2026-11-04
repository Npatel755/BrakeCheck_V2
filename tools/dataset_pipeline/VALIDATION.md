# Dataset pipeline verification

Verified on this Windows PC on 2026-10-16. This document distinguishes tested behavior from configured or deferred work.

## Data preparation on the real supplied dataset

- Restored and SHA256-checked 2,000 photos against the supplied SQLite registry; 63 photos were absent from the exported photo folders and were recovered from original source references.
- Restored 2,000 original comma10k masks and verified the recorded Git blob hash prefixes.
- Ran source annotation, fusion, automatic lane extraction and QC on all 1,973 curated images. Source annotation: 1,973 successful, zero failed. Geometry: 1,973 written, 4,228 slotted lane boundaries. QC: zero missing geometry.
- Corrected prefixed filename session parsing. Audit: 1,969 curated recording sessions, zero cross-split session overlap, no split membership changes required. Initial split counts: train 1,586 / validation 180 / test 207.
- Preserved the immutable imported archive copy and kept working paths local to this PC. No source ZIP was changed.

## Review and export checks

The focused regression command is in `DATASET_REVIEW.md`. It covers the imported source pipeline plus new transaction, review and export behavior. New review cases cover:

- Human approval remains exact and durable across store reinitialization; model annotation cannot replace it.
- A duplicate network retry creates no second review event.
- Stale revisions and a second reviewer's active assignment are rejected.
- Corrected point geometry is used exactly, with no polynomial replacement of hand-drawn points.
- Undo restores the prior human geometry or the original pending state, including category-folder membership.
- Invalid coordinates, duplicate rows and duplicate lane slots are rejected without changing review state.
- Pending and rejected records stay out of active UFLD lists/manifests. Undo removes formerly approved files from the reviewed export.
- Reviewed no-lane negatives produce zero existence flags and empty lane text files.
- Phone access requires pairing; sessions are HttpOnly, account names are bound to sessions, cross-origin requests fail, host codes are PC-only, excessive bad codes are rate limited and unexpected Host headers fail.
- Backups contain the SQLite snapshot, the matching review event history and immutable human annotation files.
- Adopting a large-model candidate records its version and license dependency in the human annotation and UFLD manifest.
- The ResNet34 training configuration can be generated without launching training.
- Source-guided SAM2 output is excluded from independent accuracy scoring.

## Actual browser verification

`tools/check_review_ui.py` runs Edge/Chromium against its own disposable dataset. It has passed desktop display, real LAN-address phone pairing, phone viewport layout without horizontal overflow, model comparison/adoption, point dragging, correction persistence and model provenance, rejection, undo, approval, and the automatic reviewed-only UFLD export. No JavaScript page errors were recorded.

Screenshots and the machine-readable verification report are under the ignored `test-results/browser-*/` folders. These are browser tests at a mobile viewport, not a physical iPhone test. Test reviews never touch the real dataset.

The real review server has also received and persisted user review decisions. Its live counts are in the UI/registry; this document does not freeze them as zero or manufacture completed human review.

## GPU and model checks

The road-edge/editor update passed **65 tests** across the pipeline test suite. The browser verification at `test-results/browser-e933a585/verification.json` passed 24 workflow checks using disposable data, including touch tracing of separate road edges, undo of the last inserted point (including an insertion in the middle), edit undo/redo and keyboard shortcuts, pan mode, clearing paint without clearing edges, draft recovery, and lost-response recovery after both correction and approval had already committed. Retried saves did not create new review revisions. Unknown road edges retained disabled training masks in the separate export; all four painted-lane slots retained their original meaning.

The local server was restarted with the new policy endpoint and validated empty exports. A pre-update backup was retained at `BrakeCheck_Dataset/backups/20261016-223842-5d5c43/`. Comparing that snapshot with the live database after the upgrade confirmed all 2,000 image records and all 36 existing review events were unchanged. Old human labels remain road-edge `not_reviewed`, not negative road-edge examples. No real human decisions were created by these tests. Road-edge JSONL is an interchange export requiring its own training adapter/model setup; it does not claim stock UFLDv2 road-edge training compatibility.

- NVIDIA RTX 4090 detected, 23.99 GiB VRAM.
- Separate GPU environment: PyTorch 2.11.0+cu128 / TorchVision 0.26.0+cu128; CUDA and BF16 verified.
- Official SAM2.1 Hiera Large checkpoint downloaded from pinned Hugging Face revision `665f8e2ad61cf5f53d65644ff27c8ee525124610` and loaded for real inference. The image predictor has 216,924,865 parameters; the official video checkpoint also contains video-related components.
- Three real validation images completed successfully before the full candidate pass was started. The native image-encoder feature cache is reused across component prompts. Initial measured peak allocated VRAM was about 0.75 GiB in BF16. This is a large-family image model, not a tiny stand-in; low VRAM use is not a parameter-count ranking.
- The resumable SAM2.1 Large pass was launched in a hidden background process. Its current counts/errors are in `reports/model_status.json`; the initial three-image check alone was not evidence of completion.
- An initial background pass encountered a Windows file-replacement sharing error after 411 completed outputs. JSON publication now uses unique temporary files and bounded sharing-violation retries. The pass was resumed from saved outputs rather than restarting inference from scratch.
- SAM3 and DINOv3 official license texts were fetched and hashed. During initial setup the authenticated token returned 403 for their gated repositories. This was an access failure at that stage; the later SAM3 verification below supersedes that initial status.
- DINOv3 ViT-7B is reserved, not enabled as a lane predictor. No DINOv3 decoder has been trained and no 7B memory-fit claim has been made.

SAM2 is prompted with source-mask components. It is useful for annotation refinement, but comparing that output with the same source mask does not establish independent lane accuracy. The evaluation code explicitly excludes such results from independent scores. SAM3's configured candidate path receives image and text only.

## Independent SAM3 verification on 2026-10-16

Account access is now available. The official pinned SAM3 image model loaded on the RTX 4090 and completed a corrected three-image validation smoke run:

| Evidence | Actual result |
|---|---|
| Hugging Face revision | `3c879f39826c281e95690f02c7821c4de09afae7` |
| Candidate version | `sam3:3c879f39826c281e95690f02c7821c4de09afae7:7c5f989cd081` |
| Model parameters | 840,376,374 |
| Downloaded weight bytes | 3,439,938,512 |
| Weight SHA256 | `6d06f0a5f84e435071fe6603e61d0b4cc7b40e0d39d487cfd4d67d8cc11cc14a` |
| Official license snapshot SHA256 | `4dea99bfaa016e21bc860d73f344236bd1e5c4977d1a9a8fd32f822b500ae1be` |
| Images completed / failed | 3 / 0 |
| Image-encoder forwards | 3 |
| Prompt/model forwards | 24, eight prompts per photo |
| Photos with drawable painted proposals | 3 |
| Photos with at least one road-edge proposal | 2; the remaining photo had uncertain edges |
| Peak allocated GPU memory | 2.16 GiB in this run |
| Last image inference time | 1.09 seconds; this is not a dataset-wide timing average |

Inference used only each photo and text prompts for paint, road and vehicles. Metadata records `source_guided=false`, `reference_inputs=[]`, and reuse of each photo's encoded image features across prompts. Source masks, human annotations and fitted source geometry were not supplied. Predicted vehicle masks exclude occluded spans from road contours. The corrected run fixed an earlier overbroad vehicle-occlusion proposal; its exact candidate version above identifies the resulting outputs.

These results establish that independent GPU inference ran and produced saved geometry. They do not establish lane/curb accuracy. Road contours remain approximations requiring human inspection and edge-kind correction; uncertain sides stay unknown, never automatic absent negatives. Full-dataset completion must be checked separately in `reports/model_status.json`; this smoke run covers three validation photos.

New candidates are comparison-only for all curated photos, including those with existing human reviews. Teacher registration does not replace human annotations or decisions. The review UI offers separate painted-line and road-edge adoption: each action preserves the other label group, requires an explicit save, and records its own model provenance. Exact candidate-version checks reject a new save after the displayed artifact changes; identical previously committed retries remain idempotent. Retained model-license dependencies are preserved as a union, and the road-edge export carries its separate candidate provenance.

Disposable-data regression coverage exercises preservation of painted lines during edge adoption and preservation of edges during later painted-model adoption, separate provenance, license union, stale candidate versions, compatible retry fingerprints, API forwarding and export/undo restoration. The final Python regression run passed **89 tests**. The desktop/phone browser run passed all 24 checks with no JavaScript errors and no real dataset reviews modified; its report is `test-results/browser-6e233fa9/verification.json`.

The read-only visual helper `tools/check_sam3_predictions.py` rendered originals beside all three corrected validation predictions in `test-results/sam3-real-20261017T031420Z-eaca82/index.html` and `inspection.png`. Rendering reported no JavaScript errors. Visual inspection confirms that proposals can miss paint, include incorrect segments, or approximate a road contour incorrectly; these examples do not justify automatic approval. The helper performs no inference or human review actions.

The resumable, hidden SAM3 background pass was then started for all **1,973 curated photos**, reusing the three exact-version completed outputs. Progress reached 14 completed with zero failures during the post-launch check; this is a launch check, not a completion claim. Its status recorded comparison-only mode and 43 existing human reviews preserved. Current counts, failures and completion state are in `reports/model_status.json`. The live reviewer health check passed and its labeling-policy API advertised model-candidate version 2. Training remains deferred.

## Deferred work

### Authorized frozen-DINOv3 decoder launch

The user explicitly authorized starting decoder training. The official pinned ViT-7B checkpoint requires six safetensors shards (about 27 GB); the disk shortage was resolved by the user, and the resumable authenticated transfer was started. `reports/dinov3_download.json` records actual transfer progress. This is a download in progress, not evidence of loaded 7B weights or successful GPU fit.

The hidden `bcdata.dinov3_training --wait` worker was launched and survived its launcher exiting. Its preflight found 1,586 eligible training masks and 176 validation masks at that check, after excluding dropped/rejected images. The exact cohort is refreshed and frozen after downloads and GPU availability; test images are excluded and recording-session overlap is rejected. The initial decoder uses original human source masks, not the reviewer's corrected polylines. The worker checks real BF16 7B fit with three optimizer steps/four training/four validation photos before the longer 20-epoch run. Check its actual phase in `reports/dinov3_training.json`. It does not auto-promote a checkpoint or modify human reviews.

All **95 Python regression tests passed** in the GPU environment, including frozen-backbone/updated-decoder proof, dataset exclusion, trained-checkpoint save/load, HTTP partial resume, safe restart when Range is ignored, checksum rejection, and unsafe-path refusal. The desktop/phone browser check passed without JavaScript errors or real reviews modified (`test-results/browser-51a87bdd/verification.json`). CPU stand-in tests do not establish real 7B memory fit or accuracy.

The older Jupyter kernel PID 49060 was stopped at the user's explicit request after matching its process identity and creation time. The subsequent GPU reading showed 20,198 MiB free. No unrelated applications were stopped. The earlier SAM3 pass completed **1,973 / 1,973 images with zero failures** before this worker's GPU stage. Human inspection of model predictions remains necessary; UFLDv2 training is still deferred.

### Batch review repair (2026-10-17)

The deployed reviewer had newer frontend controls with an older server missing `/api/batch`. Restarted the reviewer with the current backend after making a SQLite/review backup. Fixed the reviewed-export CSV column list to include assisted trace provenance. The full Python suite passed (95 tests). A real Chromium run against disposable data passed batch loading, explicit selection, enlarged preview, saving, undo, and recovery after a successful save response was lost followed by a page reload. Recovery reused the original UUID without duplicate decisions. Evidence: `test-results/batch-6382c06c/`. No real review decisions were created by these tests. The DINOv3 worker was left running.

### DINOv3 recovery (2026-10-17)

The original run stopped with a CUDA unknown error at step 14,202. Its best saved decoder was at step 12,688 (paint IoU 0.631383, road IoU 0.937654). Recovery loads that checkpoint, preserves the frozen original cohort and previous best, and continues toward the original 31,720-step target. The legacy checkpoint lacks optimizer moments, so recovery explicitly restarts Adam at the scheduled learning rate. New epoch recovery checkpoints (`last.pt`) include optimizer and scheduler state; validation history is also written separately. Recovery does not reproduce the original random augmentation sequence. CPU tests cover legacy recovery and optimizer-state recovery; the full suite passed 95 tests. The original run remains intact.

### Windshield-view review expansion (2026-10-17)

Imported 500 previously unseen recordings from pinned comma10k revision `6c205fe4c43cc53b2b1befafb1060d0606555027`, with MIT notice and verified Git blob hashes for each photo/mask. Removed five perceptual near duplicates; 495 images entered the review queue with source-derived painted-line geometry. All 495 sessions are distinct and disjoint from the original recordings; new sessions use train/validation only. The existing test set remains unchanged. Compared the original 2,000 records with the pre-import SQLite backup: no original review-status, lock, or split changes. All 96 Python tests passed, including session and camera-view selection checks. Visual inspection of a 12-image contact sheet confirmed windshield views spanning several lighting/road conditions. These are proxy images, not verified phone/iPhone captures. A CPU fresh-model candidate pass was launched after source proposals completed; it cannot overwrite saved human geometry. DINOv3 training continued independently and now writes optimizer recovery state every 250 steps as well as at epoch boundaries.

### Zero-painted-line recheck (2026-10-17)

At the user's explicit request, reopened 276 accepted/corrected photos with zero assigned painted-line slots after backing up SQLite, review history and immutable human files. Nonempty decisions and rejected photos were excluded. Each reopen increments the image revision, clears its claim, writes an audit event, and preserves the previous geometry (including road-edge corrections) as its pending proposal. Original source masks and immutable human records remain untouched. Pending images are excluded from reviewed training exports. Disposable tests verify selection, road-edge preservation, original-file immutability, stale-request rejection, idempotent reruns and reversible reopening. Maintenance entrypoint: `tools/requeue_empty_lanes.py` (dry run by default; `--apply` makes a backup and reopens matches).

### Completed DINOv3 decoder comparison and reviewer integration (2026-10-17)

Training reached the original 31,720-step target. The best checkpoint is `run-20261017-125101/best.pt`, step 28,548, SHA256 `731910575de97a85f4416c540e37495767c69222c78db444c8df928616be111b`; its resized argmax validation IoUs were lane 0.660541 and road 0.941771.

Generated separate comparison-only candidates on the frozen 176-image validation cohort, zero failures. Compared cached native-resolution binary probability maps at a fixed threshold of 0.5 with original source masks, ignoring unknown pixels. On the same 161 lane-positive images: fresh comma 0.629264, DINO 0.613081, SAM3 0.542652 mean lane IoU. Across all 176 images road IoU was fresh comma 0.977223, DINO 0.949157, SAM3 0.920947. Results and per-image provenance: `reports/dinov3_comparison.json`. The checkpoint was selected on this validation cohort; fresh comma may have trained on these photos. This is not an independent phone benchmark, nor a lane-polyline quality score. Test images were not used to select/tune these comparisons.

Inspected six generated lane/road overlays, including night, highway and a transverse crossing; usable traces coexist with imperfect proposals, so adoption stays explicit. Visual evidence: `test-results/sam3-real-20261017T190227Z-ee00fe/`. Added the trained decoder to individual and batch model selection with exact checkpoint/backbone provenance and both DINO license dependencies. All 99 Python tests passed. Disposable Chromium checks passed DINO batch adoption, license provenance, ordinary batch saving/undo, and interrupted-response UUID recovery (`test-results/batch-24bab472/`). Backed up reviews and restarted the reviewer. A separate GPU pass generates candidates for the full curated dataset; current human labels remain authoritative. No UFLDv2 training was started.

### Authorized provisional UFLDv2 baseline (2026-10-17)

The user explicitly authorized training a provisional baseline on current accepted labels. Pinned upstream `cfzd/Ultra-Fast-Lane-Detection-v2` at `c903880678454dfd9b55a63022368db05c00bc6d`. The only vendor patch defers importing DALI until the DALI loader is requested; the actual CULane parsingNet architecture and focal/mean/existence loss implementations remain upstream. The native Windows loader uses exported 4-slot geometry, a 1600×320 normalized bottom crop, affine augmentation and row/column targets without adding bottom extrapolation. Empty localization targets are safely ignored while negative examples still train existence heads. The standard DALI/my_interp GPU path has not been run and this run must not be described as DALI parity.

Two real GPU optimizer steps and eight real validation images passed, finite loss, 4.884 GiB peak VRAM. The upstream network has 216,408,800 trainable parameters. Initialization is torchvision ResNet34 ImageNet1K V1 (`resnet34-b627a593.pth`, SHA256 `b627a593bcbe140c234610266fe4f8ae95ea42fc881d091c9b6052e6b1d0590f`) with random UFLDv2 lane heads; no pretrained CULane/TuSimple checkpoint. The 20-epoch job at `models/ufld_baseline/run-20261017-162828` freezes export revision 2592: train 1,752, validation 225, no test pixels/labels loaded, no session overlap. Source image/mask/line files, exact slotted caches, reviewed manifest and hashes are copied separately from the live export. Later human decisions cannot mutate this snapshot.

The complete Python suite passed 102 tests, including target gaps/slot identities, empty negatives, snapshot test exclusion and lane metric matching. After live training-status integration, 22 targeted tests and disposable Chromium batch/recovery/adoption checks passed (`test-results/batch-bddc4470/`). Reviewed data were backed up before restarting the reviewer to expose live baseline progress. Validation is a Python raster lane F1 at 30-pixel line width and IoU 0.5; it is not an official CULane benchmark score. Best/last checkpoints and optimizer state are saved after validation. No app deployment or road-edge training was authorized or performed.

### Full eligible comma10k expansion and separate comparison views (2026-10-17)

Planned 6,520 additional original road-facing images, one per new recording session, pinned revision `6c205fe4c43cc53b2b1befafb1060d0606555027`, 4.689 GB including community-approved masks. Audited 24 deterministic train/val source conversions; inspected a six-photo contact sheet. The 97.2% mean sampled-point/source-paint consistency is only a self-consistency diagnostic, not human annotation accuracy. Two source masks had paint but no slotted line, including short/crossing paint. Kept human review required. The resumable import now publishes prepared chunks with source geometry plus independent CPU fresh candidates before making new images reviewable. A regression test caught and fixed an intra-import exact duplicate dropping a previously prepared pending image. A separate cache test verifies missing probability maps are regenerated without replacing human labels.

Split comparison canvas implementation separates source-mask/model overlays from the editable human/proposal labels. Source/model toggles do not change editable canvas pixels; reference clicks do not edit geometry. Shared zoom/scroll, point drag/undo, optional reference collapse, desktop side-by-side layout and phone stacking passed a disposable Chromium check (`test-results/split-576b81ce/`). Existing desktop/LAN phone pairing, editing, adoption, draft/retry recovery and separate export checks also passed (`test-results/browser-5cb146cb/`), as did batch approval/undo/retry/DINO adoption (`test-results/batch-06bf2079/`). Full Python suite: 103 passed; subsequent targeted import/cache suite: 3 passed. Backed up current reviews and restarted the reviewer; live source-mask availability verified. The larger import continues as a hidden worker and must not be reported complete until its report reaches `ready`.

### Remaining stages

- Human review of the remaining queue.
- Measured annotation quality on validation. SAM3 generation is complete; the successful GPU smoke run and later full-pass completion are documented above.
- Selection of prompts or models using validation, followed by a frozen final test evaluation.
- GPU DALI/my_interp loader execution, UFLDv2 training, model selection and independent BrakeCheck-footage evaluation.
- Core ML export/parity, phone inference integration, device benchmarking and IPA packaging.

The phone reviewer is a local web app with a home-screen shortcut. It is not a signed native iOS application or an offline review queue. No training or public deployment was run.

2026-10-17 source audit queue: full suite 108 passed; subsequent queue/detail/batch changes passed 23 focused tests. Real Chromium batch regression passed (test-results/batch-f1f4f682) and split-view regression passed (test-results/split-0b5ba625), using disposable data only. Live dataset backed up at backups/20261017-210703-904b83 before triage. Applied 64 sample, 904 flagged, 5531 awaiting_audit across 24 strata. Human review statuses/history unchanged. No unsampled source conversions eligible before actual sample approvals. Restarted reviewer on port 8765; no training restarted by this change.

2026-10-17 converter repair checks: independent source sample evaluation improved same-slot 20px raster F1 from 88.80% to 90.00% on 63 nonrejected development samples, with all 79 lane matches in 46 approved samples preserved. This is converter development evidence, not held-out model accuracy. Ten converter regression tests cover supported joins, neighboring/crossing lines, unsupported masks, stop lines/arrows, short candidates, no-op preservation and lane identity. Full suite before final integration: 129 passed. Subsequent focused converter/gate integration: 26 passed. Disposable browser artifact test-results/source-repair-7d8f1802 passed desktop/phone fresh-audit controls, provenance, archived warning removal and sample-gated release. Source-refined candidate adoption tests verify explicit adoption, version checks, preserved source geometry and no additional model license. Live source repair was prepared after backup backups/20261017-215127-0be5d9; benchmark run-20261017-214020 remained running and frozen.

Source repair applied 2026-10-17 at 22:03 EDT after backup backups/20261017-220309-bde6c0. All 4,576 held records were installed as one transaction with archived prior check/annotation/QC records: 3,608 awaiting audit, 904 flagged, 64 fresh samples across six risk cohorts. 797 optional source-refined comparison proposals published. No automatic release. Before/after fingerprints matched for image decisions/locks/splits, all review events, human annotation records, source annotation records, and the running benchmark manifest. Evidence: reports/source_repair_before.json and source_repair_after.json. Final full suite 132 passed, then 18 focused tests passed after hiding historical group warnings when their only remaining cases are individually flagged. Browser repair regression source-repair-7552e9ed and batch regression batch-2c952969 passed using disposable data. Live reviewer restarted and health checked; UFLDv2 worker continued running on unchanged snapshot. Future imports now use risk-cohort sampling and unresolved-fragment flags by default.

Future sampling preference implemented: three samples per audit group by default, all available images in smaller groups, and the group minimum overrides a smaller total target. Existing audit assignments untouched. Twenty focused source validation/repair tests passed, including protection against group release after only two of three approvals.

2026-10-17 post-audit benchmark queued by user request. Current run run-20261017-214020 continues to 20 epochs. Frozen next run run-20261017-221352-post-audit contains 3,437 train / 791 validation images at export revision 3132, including 1,957 source-validated train/validation records; test images excluded. Original manifest SHA verified unchanged. Persistent queue request reports/ufld_queue/20261017-221533-3e7ac135/request.json has a live hidden CPU worker waiting for verified predecessor completion and GPU-lock availability. The next run starts fresh with the same ResNet34/ImageNet recipe and 20 epochs; it does not mutate or resume the old dataset. Dataset manifest 690c9c0c0ab98541fa885eb23d0943a90f9c03470e7ddb97a2d379c9f876f539. Queue and prepared/resume snapshot tests: 13 passed. Desktop/phone queued benchmark status browser check passed (test-results/source-repair-cc91916c). Reviewer restarted; training unaffected. Held conversion groups remain excluded.

2026-10-17 V3 continuation repair applied to 2,606 previously held records after backup backups/20261017-223042-ed6206. Batch comma-source-repair-audit-v1:ccc1a3e65263f7e1: 37 source geometries changed, 2,569 preserved; 52 fresh samples across 19 cohorts (16 groups of three, one pair, two singletons), five flagged, 2,549 awaiting audits. Thirteen optional source-refined candidates published. Independent read-only development evaluation covered 127 accepted actual audit samples: both current failures match all seven lanes, zero metric regressions across 108 approved samples. Source endpoints stop at observed paint even when human endpoints extend farther. This is converter regression evidence, not held-out UFLDv2 accuracy. Full suite 176 passed, plus two new deployment-evidence guard tests passed. Desktop/phone browser source-repair-ef103a18 passed. Before/after evidence reports/source_v3_before.json and source_v3_after.json confirms unchanged image decisions/locks/splits, all review events, all human annotation records and 2,791 human annotation files, source annotation records, and both old/new benchmark manifests. The queued benchmark automatically started and continued on its frozen 3,437 train / 791 validation snapshot. New repaired rows remain held until actual sample approvals. No real human reviews were written by tests or repair installation.

Reviewer restart recovery: health and stats now tolerate missing, malformed, NUL-filled, or non-object optional progress reports without touching those files. An unreadable training report displays status unavailable rather than blocking access to saved annotations. Seven malformed-report/API regression cases plus existing review tests passed (26 tests total). Live reviewer health, stats and page returned successfully on port8765; saved review count2549. No training/queue worker was restarted and no annotation decisions were changed.


2026-10-17 expanded-data UFLDv2 continuation: refreshed source gates after the real human audits and validated export revision 3215 (6,446 eligible images: 5,453 train / 798 validation / 195 reserved test). Held or flagged conversions remain excluded. New run run-20261017-225943 freezes train/val manifest SHA256 a432d93e1fe342c38bfd5fca9d8dc3db096be5ef5a807f46bc450503bc4f5d57. It strictly loads learned model weights from interrupted run run-20261017-221352-post-audit/best.pt (epoch 5 / step 540, SHA256 8d42fc57a7f8ec68f9bd3f2597bab86bc9217ade62251981bb4c69890492b211), using memory-mapped CPU loading. All model/optimizer tensors in the parent checkpoint were independently checked finite. The new optimizer, 20-epoch schedule, history and best validation metric reset for the expanded cohort; previous run files remain intact. Network/anchor/input compatibility, parent manifest integrity and cross-snapshot split separation are checked before fitting. No parent-trained IDs/sessions entered evaluation and no reserved test IDs entered fitting/validation.

Pipeline suite `python -m pytest tests -q`: 210 passed, including 28 baseline/warm-start tests using disposable data. Launcher PowerShell syntax and Python compilation passed. Unscoped pytest also collected a vendor dataset-preparation utility requiring imagesize; it is outside the pipeline test suite. Real RTX 4090 continuation reached epoch 1 / 29 optimizer steps with finite loss and 4.884 GiB peak allocated VRAM. Progress is published in reports/ufld_training.json and the existing reviewer. No new validation accuracy is claimed before a completed validation pass; the validation cohort differs from the prior run. Training remains provisional, with no automatic deployment or model promotion.


2026-10-18 V4 source association repair installed after backup backups/20261017-235325-ef779d. Batch comma-source-repair-audit-v1:5ac069e16cdeb022: all585 held conversions rechecked,19 geometries changed/566 preserved,12 fresh human audit samples across6groups,530 awaiting those samples,43 individual QC flags. Final code SHA f8f4861a8e8b05a2f574b0a4ed147bb2ad0bc9368e1441abe55b2885000a5718; report reports/source_v4_evaluation-20261018-040015/report.json SHA8560100b207f3d30e629cfddc046f4a97fa6706c79cc7b4ca979812a6d621cac. All154 approved development controls retain328 same-slot matches with0regressions. Root visual QA caught nonoverlapping curved/intersection fragments; new coexisting source-paint/separation guard keeps both old slot sets and flags ambiguity, with global unsupported inward promotions0. Full pipeline suite262passed, plus10 latest independent evaluator metric/safety tests passed. Deployment tests protect human history, live claims, stale/tampered files, idempotence and fresh3sample gates. Final12sample visual check and live API V4/sample/fresh-human-required check passed. Before/after evidence reports/source_v4_install-20261017-2353/{before,after}.json confirms all protected image/review/human/source records/files and frozen UFLDv2 metadata unchanged. Reviewer stayed online and training continued; no assistant human approvals or automatic group release.
