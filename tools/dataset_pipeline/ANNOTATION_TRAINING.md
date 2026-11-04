# Updating the annotation models

New original Audi and comma10k semantic masks can improve the paint/road proposal models independently of the UFLDv2 benchmark. A flagged conversion can still supply a correct original pixel mask: the conversion flag concerns converting paint into longitudinal four-slot lane geometry. Training a segmenter does not resolve lane-slot association by itself.

Both update workers train isolated candidates. They never write review decisions, replace source labels, change the source-audit comparison model, or publish predictions into the reviewer automatically.

## Frozen data

`bcdata.source_mask_training.freeze_source_cohort(settings, registry, output_dir)` freezes curated commercial-pool original human semantic masks from comma10k and Audi. It excludes rejected/dropped images and reserved test data, and checks recording-session separation across train/validation/test. It records original annotation rows, image/mask/palette/calibration/provenance hashes, taxonomy code, and data licence snapshots. Later imports do not change an existing training cohort.

`BrakeCheck_Dataset/reports/annotation_update-latest.json` points to the most recently prepared shared cohort. The October 18 update uses 16,085 training images (8,375 Audi + 7,710 comma) and 1,758 validation images (895 Audi + 863 comma).

Audi unmapped classes remain ignored. Paint includes only the original mapped solid/dashed paint classes; road-edge polylines and human lane corrections are not substituted for dense semantic masks. Source-labelled transverse paint can be a semantic paint target, but still must be filtered when creating longitudinal UFLDv2 boundaries.

## Candidate workers

From the project root, provide the frozen manifest path as `-Cohort`:

```powershell
.\tools\dataset_pipeline\Start-Annotation-Training.ps1 -Cohort '<manifest path>' -Epochs 8
.\tools\dataset_pipeline\Start-DINOv3-Training.ps1 -Cohort '<manifest path>' -WarmStart '<existing best.pt>' -Epochs 6
```

The road/paint candidate fine-tunes the cached, pinned MIT comma U-Net/EfficientNet-B2 weights at 1024 x 512, with source-balanced sampling, weighted cross-entropy and paint Dice loss. It performs a real GPU forward/backward/optimizer check, restores the original weights, evaluates that baseline, then trains up to eight epochs with early stopping. The original five output classes and RGB 0..255 input convention are preserved.

The DINOv3 candidate retains the official pinned 7B frozen backbone and trains its existing decoder at 896 x 448, batch size one, BF16. `-WarmStart` initializes decoder weights and starts a new optimizer/schedule/cohort; `-Resume` continues an existing experiment with its original cohort. These are mutually exclusive. A same-profile measured prior GPU peak may justify the recorded free-memory guard; a new warm-start still runs a real three-step fit check. The GPU lock is released while waiting for memory.

Workers share `logs/large-models.lock`, so only one GPU experiment runs at a time. Other applications are not stopped. Progress and results are in `reports/annotation_training.json` and `reports/dinov3_training.json`; checkpoints are in separate versioned model folders.

## Comparison and adoption

Each worker evaluates its old and new weights on exactly the same frozen validation photos at that worker's input size. It reports separate Audi and comma paint/road results and chooses checkpoints using held-out Audi validation drives. The comma pretrained segmenter has already seen comma10k, so its comma validation result is a diagnostic, not an independent unseen-data accuracy claim. Reserved test data stays unused.

Compare paint IoU, precision/recall and road overlap, inspect difficult examples, and retain the old model if the candidate does not help. These segmentation metrics are different from the UFLDv2 lane benchmark. Candidates need an explicit adoption step; their training does not re-open or overwrite saved human annotations or count as human audit approval. The source-audit independent comparison model remains pinned during this import.

Data and model licence conditions are copied into each run. Audi-based candidates are for this private local development workflow; model distribution needs its own rights check under the recorded conditions.

## Phone review and deferred photos

The reviewer automatically uses a compact phone layout below 820 px (or with ?phone=1): source/model reference, painted-line view, Exclude/Approve/Skip, and Use model lanes. Adopting model lanes creates an editable correction draft; Approve saves that correction through the same validated desktop API. Pairing can be retried immediately with the current PC code.

Skip stores a persistent Review later entry in the PC registry. It does not create a human decision, pass an audit, or release a source group. Deferred source records are held out of new UFLDv2 exports. Use the Review later filter to review them, or Return to review queue on desktop. Saved decisions clear the deferral. Local unfinished drafts stay on the device where they were drawn. Existing frozen training experiments remain unchanged.
