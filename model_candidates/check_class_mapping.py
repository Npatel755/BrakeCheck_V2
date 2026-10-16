"""Compare checkpoint argmax classes with three original comma10k masks."""

import json
from pathlib import Path

import numpy as np
from PIL import Image
import segmentation_models_pytorch as smp
from safetensors.torch import load_file
import torch


ROOT = Path(__file__).resolve().parent


def main():
    params = json.loads((ROOT / "comma10k-segnet/config.json").read_text())
    params.pop("_model_class")
    model = smp.Unet(**params).eval()
    model.load_state_dict(load_file(ROOT / "comma10k-segnet/model.safetensors"), strict=True)
    totals = {}
    for index in range(3):
        image = Image.open(ROOT / f"evaluation-samples/image-{index}.png").convert("RGB").resize((512, 384))
        mask = Image.open(ROOT / f"evaluation-samples/mask-{index}.png").convert("RGB").resize((512, 384), Image.Resampling.NEAREST)
        pixels = np.array(image)
        target = np.array(mask)
        with torch.inference_mode():
            scores = model(torch.from_numpy(pixels.copy()).permute(2, 0, 1).unsqueeze(0).float())
            predicted = scores.argmax(1)[0].numpy()
        for rgb in np.unique(target.reshape(-1, 3), axis=0):
            key = tuple(int(channel) for channel in rgb)
            matching = np.all(target == rgb, axis=2)
            counts = np.bincount(predicted[matching], minlength=5)
            totals[key] = totals.get(key, np.zeros(5, dtype=np.int64)) + counts
    for key, counts in sorted(totals.items(), key=lambda item: -item[1].sum()):
        print(key, counts.tolist())


if __name__ == "__main__":
    main()
