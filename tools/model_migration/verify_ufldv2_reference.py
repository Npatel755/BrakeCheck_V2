"""Compare CPU logits across original and converter PyTorch environments.

Run --write-reference in the converter environment, then run without that
flag in the original environment using the same --reference NPZ. No training
or reserved-test images are used. This does not execute Core ML.
"""
import argparse
import json
from pathlib import Path
import platform

import numpy as np
import torch
import torchvision

from export_ufldv2 import DEFAULT_RUN, EXAMPLE_IDS, EXPECTED_CHECKPOINT, HEADS, RGBUFLDv2, load_frozen, rgb_sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--write-reference", action="store_true")
    args = parser.parse_args()
    torch.set_num_threads(4)
    torch.manual_seed(0)
    root = args.source_root.resolve()
    run = root / DEFAULT_RUN
    _, model = load_frozen(root, run)
    wrapper = RGBUFLDv2(model).eval()
    records = {record["image_id"]: record for record in json.loads((run / "dataset/manifest.json").read_text()) if record["split"] == "val"}
    reserved = set(json.loads((run / "dataset/reserved_test_ids.json").read_text()))
    environment = {"python": platform.python_version(), "torch": torch.__version__,
                   "torchvision": torchvision.__version__, "platform": platform.platform(), "device": "CPU float32"}
    values, comparisons = {}, []
    reference = None if args.write_reference else np.load(args.reference, allow_pickle=False)
    with torch.inference_mode():
        for index, image_id in enumerate(EXAMPLE_IDS):
            assert image_id not in reserved
            outputs = wrapper(rgb_sample(run / "dataset", records[image_id]))
            result = {"image_id": image_id, "split": "val", "heads": {}}
            for name, value in zip(HEADS, outputs):
                key = str(index) + "_" + name
                assert torch.isfinite(value).all()
                if args.write_reference:
                    values[key] = value.numpy()
                else:
                    other = torch.from_numpy(reference[key].copy())
                    torch.testing.assert_close(value, other, atol=1e-3, rtol=1e-4)
                    result["heads"][name] = {"max_abs_error": float((value - other).abs().max()),
                        "mean_abs_error": float((value - other).abs().mean())}
            comparisons.append(result)
            print(json.dumps({"phase": "cross_framework_reference" if args.write_reference else "cross_framework_compare", "image_id": image_id}), flush=True)
    if args.write_reference:
        np.savez(args.reference, **values)
        args.reference.with_suffix(".json").write_text(json.dumps(environment, indent=2) + "\n")
    else:
        report = {"checkpoint_sha256": EXPECTED_CHECKPOINT,
            "reference_environment": json.loads(args.reference.with_suffix(".json").read_text()),
            "comparison_environment": environment, "atol": 1e-3, "rtol": 1e-4,
            "reserved_test_images_used": 0, "samples": comparisons,
            "status": "PASS", "coreml_runtime_executed": False}
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": "PASS", "samples": len(comparisons),
            "max_abs_error": max(head["max_abs_error"] for sample in comparisons for head in sample["heads"].values())}), flush=True)


if __name__ == "__main__":
    main()
