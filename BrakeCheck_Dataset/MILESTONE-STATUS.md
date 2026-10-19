# Dataset pipeline status — 2026-10-16

## Implemented

- Isolated `BrakeCheck_Dataset/` workspace with SQLite metadata, pinned
  comma10k acquisition, source/license registry, SHA-256 and Git blob checks,
  exact and perceptual duplicate flags, quality flags, and deterministic
  session-based train/validation/test splits.
- Rights-gated video extractor tested on a synthetic recording. It samples
  baseline frames at 1 fps plus significant intervening scene changes. No
  user-provided or third-party video was added to the commercial dataset.
- Pinned MIT `comma10k-segnet` baseline producing provisional road and
  lane-marking masks, overlays, model provenance, and a disagreement-prioritized
  review queue. Original human masks are never overwritten.
- Local color-first review gallery with original frame, AI overlay and
  original human-label overlay on each of 100 cards.

## Real run

Source: [comma10k](https://github.com/commaai/comma10k), commit
`6c205fe4c43cc53b2b1befafb1060d0606555027`. The repository states the
images and labels are MIT licensed; the upstream notice is stored under
`sources/comma10k-LICENSE.txt`.

- Planned/downloaded: 100 image/mask pairs; 69,695,736 source bytes.
- Imported: 100 images, 100 original masks, 100 unique image SHA-256 values.
- Splits: 70 train, 15 validation, 15 test; zero sessions span splits.
- Quality flags: 6 dark-review, 5 blur-review. These images were retained.
- Provisional model outputs: 100 lane masks, 100 road masks, 100 overlays.
- Sequential CPU processing of 99 remaining images: 27.3 seconds; rerun
  skipped all 100 in 0.96 seconds. The pinned checkpoint hash is
  `8208672861ad1b111dc98f3a7c54196d29875b709c7353e2dd1b7614343fb3a8`.
- Source-mask agreement: lane IoU 0.6496, road IoU 0.9586; review priorities
  7 high, 85 medium, 1 random QC, 7 low. These are **not** independent model
  accuracy estimates because comma10k was used to train the checkpoint.
- Eight local unit tests pass. Ingestion rerun preserved all 100 rows and image
  hashes. A second annotation run skipped all 100 existing predictions.

Commands executed: `python -m unittest discover -s tests -v`,
`python -m brakecheck_dataset.cli plan-comma10k --count 100`,
`python -m brakecheck_dataset.cli fetch-comma10k`,
`python -m brakecheck_dataset.cli fetch-comma-model`,
`python -m brakecheck_dataset.cli annotate-comma10k --limit 100`, and
`python -m brakecheck_dataset.cli review-queue`, and
`python -m brakecheck_dataset.cli build-gallery` (the model command used the
existing WSL Python environment).

## Remaining work

- Verify separate rights and access for comma2k19 recordings, CARLA assets,
  ZOD data, and each SAM 3/DINOv3 checkpoint before use. These are quarantined.
- Add independent authorized driving sessions, meaningful scene clustering,
  and a fixed human-reviewed evaluation set. A large set of similar frames
  would not establish field performance.
- Build lane-instance separation and visible-line geometry, then inspect the
  actual UFLD V2 loader before implementing a training export.
- Integrate CVAT editing/approval, preserve reviewer history, and measure correction
  rates. No provisional output has been promoted to verified ground truth.
- Add a standalone FFmpeg extractor, optional CARLA generation, future ZOD
  adapter, dashboard, and active-learning loop.
- The machine reported an RTX 4050 with 6 GB VRAM, rather than the 4090/24 GB
  specified in the planning document. The current model run used CPU and 4
  threads; any larger teacher needs a measured memory plan before installation.
