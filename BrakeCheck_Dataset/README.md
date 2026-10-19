# BrakeCheck Dataset pipeline

**To look at the images:** open [the local review gallery](review_gallery/index.html)
in a browser. It shows the original color photo, the AI candidate overlay,
and the existing human labels side by side. Use its priority filters to start
with the most disputed lanes. Files ending in `-lane.png` or `-road.png` are
binary training masks, so they are intentionally black and white and have no
camera background.

This folder is isolated from the iOS app and model-candidate worktree. It starts
the dataset plan's first milestone: source clearance, pinned acquisition,
integrity checks, duplicate detection, session-based splits, and provenance.
The data directory and SQLite registry are local; large data is ignored by Git.

Run from this folder:

```powershell
python -m pip install -r requirements.txt
python -m brakecheck_dataset.cli init
python -m brakecheck_dataset.cli sources
python -m brakecheck_dataset.cli plan-comma10k --count 100
python -m brakecheck_dataset.cli fetch-comma10k
python -m unittest discover -s tests -v
```

The first provisional teacher and source-agreement queue can be run with the
existing WSL model environment:

```bash
cd /mnt/c/Users/nickp/OneDrive/Desktop/BrakeCheck_V2/BrakeCheck_Dataset
/home/nickp/ar-model-venv/bin/python3 -m brakecheck_dataset.cli annotate-comma10k --limit 100
/home/nickp/ar-model-venv/bin/python3 -m brakecheck_dataset.cli review-queue
/home/nickp/ar-model-venv/bin/python3 -m brakecheck_dataset.cli build-gallery
```

On a fresh checkout, run `python -m brakecheck_dataset.cli fetch-comma-model`
first. It retrieves only the pinned checkpoint, verifies its SHA-256, and
checks the model card's MIT tag. The checkpoint is ignored by Git.

The comma.ai checkpoint is pinned by SHA-256 in `config/models.json`. Predictions
are marked provisional in SQLite and written under `annotations/teacher_predictions`;
the original human masks remain under `raw/comma10k/masks`. The source-agreement
report and review queue are for spotting likely errors, not a held-out benchmark:
the checkpoint was trained on comma10k. See [milestone status](MILESTONE-STATUS.md)
for measured results and remaining work.

`plan-comma10k` records a pinned source commit and the exact expected bytes.
`fetch-comma10k` has a 5 GB hard cap, checks each file against its Git blob hash,
and resumes by skipping verified files. Original masks remain unmodified. The
database records source, session, hashes, split, quality flags and provenance.
Near duplicates are flagged for review instead of silently removed. Dark or
rainy frames are not rejected merely for their lighting.

The `extract-video` command accepts only a source registered as a cleared
`video` source in `config/sources.json`. For a recording you own or have a
commercial-use grant for, add a source entry with its specific rights evidence
before running:

```powershell
python -m brakecheck_dataset.cli extract-video --source YOUR_CLEARED_SOURCE --video C:\path\drive.mp4 --session drive-001
```

It samples one baseline frame per second and checks intervening half-second
frames for significant scene changes. It keeps source-video hashes and assigns
one train/validation/test split to the entire session. The installed OpenCV
build currently performs decoding; a standalone FFmpeg backend is still to be
added.

Only comma10k is currently cleared in [the source registry](config/sources.json).
The repository [README](https://github.com/commaai/comma10k) explicitly says
the images and labels are MIT licensed, and its [license](https://github.com/commaai/comma10k/blob/master/LICENSE)
requires retaining the notice. comma2k19 recordings, CARLA assets, and ZOD
remain quarantined pending source-specific terms and access checks. A GitHub
repository's code license is not automatically a license to every external
dataset download.

This first stage does **not** claim that semantic comma10k masks are UFLD V2
lane instances. The comma.ai model provides provisional semantic annotations;
reviewer tooling, lane geometry, and UFLD V2 export remain future milestones
and require independent validation.
