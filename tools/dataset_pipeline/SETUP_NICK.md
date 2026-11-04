# Running this on the RTX 4090 desktop

Written for Nick. Harrison's PC has no GPU, so everything that needed one was written and tested on small CPU stand-ins, but not yet run for real. The "Status" table at the bottom says exactly what has been verified.

## 1. One-time setup (WSL2 Ubuntu)

```bash
# in WSL2 Ubuntu
sudo apt update && sudo apt install -y python3.12-venv ffmpeg git
nvidia-smi                                   # the 4090 should show up inside WSL
cd ~ && unzip /mnt/c/path/to/brakecheck-dataset.zip && cd brakecheck-dataset
python3.12 -m venv .venv && . .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
python -m pytest tests -q                    # expect: 23 passed
```

Keep the dataset on the WSL ext4 disk (`~/...`), not `/mnt/c`. It is many times faster, and comma2k19 folder names contain `|`, which Windows can't store.

## 2. Gated model access (free, needs a Hugging Face / Meta account)

| Model | Where to request | Then |
|---|---|---|
| DINOv3 (backbone for our own teacher) | https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m (accept the licence) | `hf auth login` |
| SAM 3 | https://huggingface.co/facebook/sam3 | `git clone https://github.com/facebookresearch/sam3 && pip install -e sam3` |
| SAM 2.1 (not gated) | `wget https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_large.pt -P BrakeCheck_Dataset/models/sam2/` | `git clone https://github.com/facebookresearch/sam2 && pip install -e sam2` |

The licence terms are recorded in [config/licenses.yaml](config/licenses.yaml):
- DINOv3 and SAM 3 allow commercial use, with conditions: no military or ITAR use, and ship the licence text with anything redistributed.
- SAM 2.1 is Apache-2.0.

## 3. Run the milestones

The 2,000-image comma10k base set is already built on Harrison's PC (see `reports/`). To rebuild or extend it on the GPU machine:

```bash
python -m bcdata run-all --target 2000 --ufld-repo ~/Ultra-Fast-Lane-Detection-v2     # M1-M4 + exports + report
```

**Milestone 3: our DINOv3 teacher.**

```bash
python -m bcdata train-teacher --epochs 20 --batch-size 4 --promote     # ViT-L, ~30-60 min on a 4090 for ~1,700 train images
# then enable it:  teachers.dinov3_seg.enabled: true   in config/pipeline.yaml
python -m bcdata annotate --teachers dinov3_seg
```

**SAM 3 and the SAM 2.1 refinement.**

```bash
# set teachers.sam3.enabled / teachers.sam2_refine.enabled: true
python -m bcdata annotate --teachers sam3,sam2_refine
python -m bcdata evaluate        # compares comma_segnet, dinov3_seg, sam3, sam2_refine on the same test images
```

Only raise a teacher's weight in `fusion.weights` (sam2_refine starts at 0) when `evaluate` shows it is better on the test split.

**After that, the normal loop.**

```bash
python -m bcdata fuse && python -m bcdata geometry && python -m bcdata qc
python -m bcdata queue                    # writes review_queue/<batch>/cvat_task/
```

## 4. CVAT (two reviewers)

```bash
git clone https://github.com/cvat-ai/cvat && cd cvat
docker compose up -d
docker exec -it cvat_server bash -ic 'python3 ~/manage.py createsuperuser'
# open http://localhost:8080, create accounts for both reviewers
```

Follow `review_queue/<batch>/cvat_task/README.txt`. In short:
1. Create a task with `labels.json` (paste into Raw) and upload `images/`.
2. Upload `annotations.xml` as **CVAT 1.1**.
3. Reviewers fix the red `lane` lines and tag each image `review_approved`, `review_rejected` or `review_difficult`.
4. Export as **CVAT for images 1.1** and import the result:

```bash
python -m bcdata review-import --batch BrakeCheck_Dataset/review_queue/<batch> --xml <exported.xml> --reviewer <name> --minutes <time spent>
```

The first queue puts the whole `test` split first. Reviewing it turns it into the human-checked evaluation set every later comparison uses.

**Active learning, once at least 100 new images are reviewed:**

```bash
python -m bcdata al-cycle
```

It fine-tunes the DINOv3 teacher on reviewed data, re-scores it on the fixed test split, and promotes it only if lane IoU improves without any scene tag regressing. It then re-labels the uncertain images and writes the next queue.

## 5. Train UFLD V2 on the export

```bash
git clone https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2 ~/Ultra-Fast-Lane-Detection-v2
# follow its INSTALL.md (DALI, my_interp)
python tools/check_ufld_dali.py --ufld ~/Ultra-Fast-Lane-Detection-v2 \
   --config BrakeCheck_Dataset/exports/ufld_v2/BrakeCheck_CULane/configs/brakecheck_culane_res18.py
cd ~/Ultra-Fast-Lane-Detection-v2 && python train.py <that config>
```

**Do not fine-tune from UFLD's released CULane/TuSimple/CurveLanes weights for the App Store model.** Those datasets are non-commercial. The generated config already sets `finetune=None`.

## Status: what has and hasn't been verified

| Part | Status |
|---|---|
| comma10k ingest (git-hash verified), quality checks, curation, leak-free splits | Run on real data (2,000 images) |
| comma10k-segnet teacher | Run on real data (CPU); agrees with the human masks at lane IoU ~0.68 |
| Fusion, mask→lane geometry, QC, overlays | Run on real data; geometry tuned on comma10k masks (wide-angle slanted lanes, double lines, stop lines) |
| UFLD export | Validated by reading it like UFLD's iterator, and UFLD's own `cache_culane_ponits.py` reproduces our cache exactly. The DALI loader itself needs a GPU: run `tools/check_ufld_dali.py` |
| Semantic segmentation export | Run on real data |
| CVAT package and import, label locking | Round trip tested with a scripted reviewer. **Do one real CVAT import with 3 images first**: the mask RLE layout in the XML hasn't been checked against a live CVAT |
| DINOv3 decoder training | Training loop, checkpoint, inference and untrained-refusal tested with a tiny CPU backbone; the Hugging Face path tested with open DINOv2-small. **Not yet trained on real DINOv3 weights** (gated) |
| SAM 3 teacher, SAM 2.1 refinement and video propagation | Written to the official APIs, **not run** (needs GPU + access). Expect small API fixes on first run |
| comma2k19 frames | Extraction and adaptive selection tested on synthetic video; the real torrent chunk is ~10 GB and needs approval |
| CARLA generator, ZOD adapter | Written, **not run** (no simulator; no ZOD access yet) |
| Human correction rate | Not measurable until reviewers have done a batch |
