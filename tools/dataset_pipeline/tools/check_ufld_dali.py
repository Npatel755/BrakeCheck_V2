"""GPU check: load the export through UFLD V2's real DALI training iterator and pull one batch.

    python tools/check_ufld_dali.py --ufld /path/to/Ultra-Fast-Lane-Detection-v2 \
        --config BrakeCheck_Dataset/exports/ufld_v2/BrakeCheck_CULane/configs/brakecheck_culane_res18.py

Needs a CUDA GPU, NVIDIA DALI and UFLD's my_interp extension built (see UFLD INSTALL.md).
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ufld", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--batches", type=int, default=2)
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.ufld))
    os.chdir(a.ufld)
    from utils.config import Config  # type: ignore
    cfg = Config.fromfile(a.config)
    from data.constant import culane_col_anchor, culane_row_anchor  # type: ignore
    import numpy as np
    cfg.row_anchor = np.linspace(0.42, 1, cfg.num_row)          # what train.py sets for CULane
    cfg.col_anchor = np.linspace(0, 1, cfg.num_col)
    from data.dali_data import TrainCollect  # type: ignore
    loader = TrainCollect(4, 4, cfg.data_root, os.path.join(cfg.data_root, "list/train_gt.txt"), 0, 1,
                          cfg.row_anchor, cfg.col_anchor, cfg.train_width, cfg.train_height, cfg.num_cell_row,
                          cfg.num_cell_col, cfg.dataset, cfg.crop_ratio)
    for i, batch in enumerate(loader):
        lr = batch["labels_row"]
        print(f"batch {i}: images {tuple(batch['images'].shape)} labels_row {tuple(lr.shape)} "
              f"valid row labels {(lr >= 0).float().mean().item():.3f}")
        if i + 1 >= a.batches:
            break
    print("UFLD DALI loader OK")


if __name__ == "__main__":
    main()
