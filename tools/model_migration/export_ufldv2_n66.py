"""Export the trusted, frozen BrakeCheck UFLDv2 checkpoint for iOS 17+.

This script does not download weights, train, change annotations. N66 uses recall v4 for display only; the driving corridor
retains the original stock decoder. Linux exports MLProgram packages;
Core ML execution parity and performance still require an Apple device.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import cv2
import numpy as np
import torch

EXPECTED_CHECKPOINT = "4838156c4bd7c237d9189ff617e150f38d96f5018b48ac53e472ea7d4b23f378"
EXPECTED_MANIFEST = "7327866bf96b4247cebce245c058b4bb9a72c6a19234a489001d36ff44153ac5"
EXPECTED_TRAINING_CODE = "5b88b3123c612e62997204666b71357ed4589163ab514c15f91eaaab6234b258"
DEFAULT_RUN = "BrakeCheck_Dataset/models/ufld_baseline/run-20261020-142147-post-n62-label-fixes"
WORKTREE = Path(__file__).resolve().parents[2]
HEADS = {"loc_row": [1, 200, 72, 4], "loc_col": [1, 100, 81, 4],
         "exist_row": [1, 2, 72, 4], "exist_col": [1, 2, 81, 4]}
EXAMPLE_IDS = ["a2d2-018f2db6dd980480", "a2d2-43d8159b90823986",
               "a2d2-cfde01ecfd7b5712", "comma10k-98f0954ccd00d7b5",
               "comma10k-234edcb80f4e2715", "comma10k-5c42aa19ae71f518",
               "comma10k-3495bfeac5e25a51", "comma10k-3226e9962962c93a"]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_frozen(source_root, run):
    """Verify provenance before unpickling our trusted local checkpoint."""
    assert sha(run / "best.pt") == EXPECTED_CHECKPOINT, "Checkpoint changed"
    assert sha(run / "dataset/manifest.json") == EXPECTED_MANIFEST, "Snapshot changed"
    assert sha(run / "training_code.py") == EXPECTED_TRAINING_CODE, "Training code changed"
    sys.path.insert(0, str(source_root / "tools/dataset_pipeline"))
    name = "bcdata._frozen_ufld_mobile_export"
    spec = importlib.util.spec_from_file_location(name, run / "training_code.py")
    frozen = importlib.util.module_from_spec(spec)
    sys.modules[name] = frozen
    spec.loader.exec_module(frozen)
    vendor = source_root / "tools/dataset_pipeline/vendor/ufld_v2"
    # The Windows vendor checkout uses CRLF. Normalize it as Git for Windows
    # does when comparing the frozen training patch from a Linux converter.
    git = ["git", "-c", "safe.directory=" + str(vendor), "-c", "core.autocrlf=true", "-C", str(vendor)]
    assert subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip() == frozen.UPSTREAM
    assert subprocess.check_output(git + ["diff"], text=True) == (run / "upstream.patch").read_text()
    saved = torch.load(run / "best.pt", map_location="cpu", mmap=True, weights_only=False)
    frozen.validate_warm_start_config(saved["config"])
    assert saved["epoch"] + 1 == 3
    model = frozen.upstream_model(vendor, pretrained=False)
    model.load_state_dict(saved["model"], strict=True)
    model.eval()
    del saved
    return frozen, model


class RGBUFLDv2(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.net = model
        self.register_buffer("rgb_mean", torch.tensor([.485, .456, .406]).reshape(1, 3, 1, 1))
        self.register_buffer("rgb_std", torch.tensor([.229, .224, .225]).reshape(1, 3, 1, 1))

    def forward(self, image):
        pred = self.net((image / 255.0 - self.rgb_mean) / self.rgb_std)
        return pred["loc_row"], pred["loc_col"], pred["exist_row"], pred["exist_col"]


def rgb_sample(snapshot, record):
    image = cv2.imread(str(snapshot / record["image"]))
    assert image is not None and image.shape[:2] == (590, 1640)
    image = cv2.resize(image, (1600, 533), interpolation=cv2.INTER_LINEAR)[213:533]
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32)
    return torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1))).unsqueeze(0)


def verify_trace(frozen, model, wrapped, traced, snapshot):
    """Real validation frames: original loader vs wrapper vs traced graph."""
    dataset = frozen.NativeDataset(snapshot, "val")
    reserved = set(json.loads((snapshot / "reserved_test_ids.json").read_text()))
    by_id = {record["image_id"]: index for index, record in enumerate(dataset.records)}
    report = []
    with torch.inference_mode():
        for image_id in EXAMPLE_IDS:
            assert image_id not in reserved
            index = by_id[image_id]
            record = dataset.records[index]
            raw = rgb_sample(snapshot, record)
            native = model(dataset[index]["images"].unsqueeze(0))
            eager = wrapped(raw)
            scripted = traced(raw)
            heads = {}
            for name, actual, expected in zip(HEADS, scripted, eager):
                assert list(actual.shape) == HEADS[name]
                assert torch.isfinite(actual).all()
                torch.testing.assert_close(actual, expected, atol=1e-5, rtol=1e-5)
                torch.testing.assert_close(expected, native[name], atol=1e-3, rtol=1e-4)
                heads[name] = {"shape": list(actual.shape),
                    "eager_vs_trace_max_abs": float((actual - expected).abs().max()),
                    "native_loader_vs_rgb_wrapper_max_abs": float((expected - native[name]).abs().max())}
            report.append({"image_id": image_id, "split": "val", "heads": heads})
            print(json.dumps({"phase": "trace_parity", "image_id": image_id}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--cross-framework-report", type=Path,
        help="Optional report produced by verify_ufldv2_reference.py in the original PyTorch environment")
    parser.add_argument("--output", type=Path, default=WORKTREE / "ios/Sources/BrakeCheck/Resources/LaneModelData/UFLDv2Lane.mlpackage")
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    run = (args.run or source_root / DEFAULT_RUN).resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("Use a new output path; existing packages are never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(4)
    torch.manual_seed(0)
    cv2.setNumThreads(1)
    import coremltools as ct
    started = time.monotonic()
    frozen, model = load_frozen(source_root, run)
    wrapped = RGBUFLDv2(model).eval()
    records = json.loads((run / "dataset/manifest.json").read_text())
    example = next(record for record in records if record["image_id"] == EXAMPLE_IDS[0] and record["split"] == "val")
    raw = rgb_sample(run / "dataset", example)
    print(json.dumps({"phase": "tracing", "checkpoint_sha256": EXPECTED_CHECKPOINT}), flush=True)
    with torch.no_grad():
        traced = torch.jit.trace(wrapped, raw, strict=True)
    parity = verify_trace(frozen, model, wrapped, traced, run / "dataset")
    print(json.dumps({"phase": "coreml_conversion", "trace_samples": len(parity)}), flush=True)
    converted = ct.convert(traced, source="pytorch",
        inputs=[ct.ImageType(name="image", shape=(1, 3, 320, 1600), color_layout=ct.colorlayout.RGB, scale=1.0)],
        outputs=[ct.TensorType(name=name, dtype=np.float32) for name in HEADS],
        convert_to="mlprogram", minimum_deployment_target=ct.target.iOS17,
        compute_precision=ct.precision.FLOAT16, skip_model_load=True)
    metadata = {
        "source": "https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2",
        "upstream_commit": frozen.UPSTREAM, "architecture": "UFLDv2 ResNet34",
        "checkpoint_sha256": EXPECTED_CHECKPOINT, "checkpoint_epoch": "3",
        "dataset_manifest_sha256": EXPECTED_MANIFEST, "export_revision": "6116",
        "license": "MIT architecture; locally trained weights; dataset provenance retained in frozen run",
        "preprocessing": "RGB byte image 1600x320; embedded ImageNet normalization. Caller resizes 1640x590 canvas to 1600x533 and crops rows 213:533.",
        "decoder": "Raw 4-head output; stock hybrid decoder for driving corridor, recall_budget_v4 for display only.",
        "validation_status": "Provisional local validation; CoreML runtime parity and device latency not yet measured",
    }
    converted.user_defined_metadata.update(metadata)
    converted.short_description = "BrakeCheck locally trained UFLDv2 ResNet34 lane boundaries (epoch 3, provisional)."
    converted.author = "BrakeCheck"
    converted.license = metadata["license"]
    converted.input_description["image"] = "Prepared RGB 1600x320 image with training bottom crop; byte values, no external normalization."
    for name in HEADS:
        converted.output_description[name] = f"Raw UFLDv2 logits {HEADS[name]} in NCHW-like [batch, bins/classes, anchors, slots] layout."
    converted.save(str(output))
    provenance = {
        "name": "UFLDv2Lane", "format": "Core ML MLProgram", "minimum_ios": "17.0",
        "precision": "float16 compute/weights, float32 logits outputs",
        "input": {"name": "image", "type": "RGB image bytes", "shape": [1, 3, 320, 1600]},
        "outputs": HEADS, "metadata": metadata,
        "source_run": DEFAULT_RUN,
        "display_policy_calibration": json.loads((source_root / "BrakeCheck_Dataset/experiments/decoder_recall/run-20261023-fp243/selected_decoder.json").read_text()), "checkpoint_epoch": 3,
        "checkpoint_sha256": EXPECTED_CHECKPOINT, "training_code_sha256": EXPECTED_TRAINING_CODE,
        "manifest_sha256": EXPECTED_MANIFEST,
        "training_canvas": {"width": 1640, "height": 590},
        "preprocessing": {"resize_width": 1600, "resize_height": 533, "crop_top": 213,
            "crop_height": 320, "mean": [.485, .456, .406], "std": [.229, .224, .225],
            "normalization_embedded": True, "color_layout": "RGB", "input_scale": 1.0},
        "decoder": {"row_anchors": frozen.ROW_ANCHORS.tolist(), "col_anchors": frozen.COL_ANCHORS.tolist(),
            "row_slots_zero_based": [1, 2], "column_slots_zero_based": [0, 3],
            "row_minimum_positive_anchors": 37, "column_minimum_positive_anchors": 21,
            "local_softargmax_radius": 1, "bin_center_offset": .5,
            "row_bins": 200, "column_bins": 100, "experimental_decoder_promoted": False, "display_policy": "recall_budget_v4",
            "display_rule": {"row_minimum": 2, "col_minimum": 2, "confidence": .7, "minimum_points": 4,
                "max_normalized_step": .15, "max_path_to_chord": 2., "max_turn_degrees": 150.}},
        "validation": {"checkpoint_metrics": {"tp": 1438, "fp": 148, "fn": 966,
                "precision": 0.9066834804539723, "recall": 0.5981697171381032, "f1": 0.7208020050125313},
            "checkpoint_metrics_scope": "Frozen 1586-image validation set using CUDA bf16; not Core ML/device accuracy",
            "eager_trace_and_preprocessing_parity": parity, "reserved_test_images_used": 0,
            "coreml_execution_parity": "Not run: converter host is Linux; requires macOS/iOS",
            "device_latency": "Not measured", "weights_changed": True, "annotations_changed": False},
        "conversion_environment": {"python": platform.python_version(), "platform": platform.platform(),
            "torch": torch.__version__, "coremltools": ct.__version__, "numpy": np.__version__, "opencv": cv2.__version__},
        "files": {str(path.relative_to(output.parent)): sha(path) for path in sorted(output.rglob("*")) if path.is_file()},
        "seconds": round(time.monotonic() - started, 2),
    }
    if args.cross_framework_report:
        cross_framework = json.loads(args.cross_framework_report.read_text())
        assert cross_framework["status"] == "PASS"
        assert cross_framework["checkpoint_sha256"] == EXPECTED_CHECKPOINT
        assert cross_framework["reserved_test_images_used"] == 0
        provenance["validation"]["cross_framework_cpu_parity"] = cross_framework
    provenance_path = output.with_name(output.stem + "-provenance.json")
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    del converted, traced, wrapped, model
    gc.collect()
    print(json.dumps({"phase": "complete", "package": str(output), "provenance": str(provenance_path),
        "package_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
        "seconds": provenance["seconds"]}), flush=True)


if __name__ == "__main__":
    main()
