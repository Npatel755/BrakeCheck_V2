"""Export Apache RTMDet weights to BrakeCheck's Vision image/NMS contract.

Runs on Linux for conversion; execution parity of Core ML needs an Apple device.
No Ultralytics code or weights are used in the exported graph.
"""
from pathlib import Path
import argparse
import hashlib
import json
import torch
import coremltools as ct
from libreyolo import LibreYOLO
import libreyolo
import yaml


class VisionRTMDet(torch.nn.Module):
    def __init__(self, net):
        super().__init__()
        self.net = net.eval()
        self.net.head.export = True
        self.register_buffer("mean", torch.tensor([103.53, 116.28, 123.675]).reshape(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([57.375, 57.12, 58.395]).reshape(1, 3, 1, 1))

    def forward(self, image):
        # Vision supplies RGB bytes. Upstream RTMDet uses normalized BGR.
        normalized = (image[:, [2, 1, 0], :, :] - self.mean) / self.std
        predictions = self.net(normalized)[0]
        xyxy = predictions[:, :4]
        center = (xyxy[:, :2] + xyxy[:, 2:]) / 2 / 640
        extent = (xyxy[:, 2:] - xyxy[:, :2]) / 640
        return predictions[:, 4:], torch.cat((center, extent), dim=1)


def export(size, output):
    torch.set_num_threads(4)
    detector = LibreYOLO(f"LibreRTMDet{size}.pt", device="cpu")
    source_file = Path("weights") / f"LibreRTMDet{size}.pt"
    source_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()
    expected_hash = {"t": "79b8a0b1e22b100e36a5591b24a1a97eae2fc3f29ad73cb4809d8c9c6cd19a52",
                     "s": "e352310fc6a8a48b862afbbeaf5ede67002fcdb4a488f87633e2a913a9ccaa85"}[size]
    assert source_hash == expected_hash, "Checkpoint changed: verify license/provenance before exporting"
    wrapper = VisionRTMDet(detector.model).eval()
    sample = torch.full((1, 3, 640, 640), 114.0)
    traced = torch.jit.trace(wrapper, sample)
    with torch.no_grad():
        expected = wrapper(sample)
        actual = traced(sample)
    for a, b in zip(expected, actual):
        torch.testing.assert_close(a, b)
    converted = ct.convert(
        traced, inputs=[ct.ImageType(name="image", shape=sample.shape, scale=1.0,
                                     color_layout=ct.colorlayout.RGB)],
        outputs=[ct.TensorType(name="raw_confidence"), ct.TensorType(name="raw_coordinates")],
        convert_to="mlprogram", minimum_deployment_target=ct.target.iOS15,
        compute_precision=ct.precision.FLOAT16,
    )
    spec = ct.proto.Model_pb2.Model()
    spec.specificationVersion = 5
    for feature in converted.get_spec().description.output:
        spec.description.input.add().CopyFrom(feature)
        result = spec.description.output.add()
        result.CopyFrom(feature)
        result.name = "confidence" if feature.name == "raw_confidence" else "coordinates"
        result.type.multiArrayType.ClearField("shape")
        ranges = result.type.multiArrayType.shapeRange.sizeRanges
        ranges.add().lowerBound = 0
        ranges[0].upperBound = -1
        ranges.add().lowerBound = 80 if result.name == "confidence" else 4
        ranges[1].upperBound = ranges[1].lowerBound
    nms = spec.nonMaximumSuppression
    nms.confidenceInputFeatureName = "raw_confidence"
    nms.coordinatesInputFeatureName = "raw_coordinates"
    nms.confidenceOutputFeatureName = "confidence"
    nms.coordinatesOutputFeatureName = "coordinates"
    nms.iouThreshold = 0.5
    nms.confidenceThreshold = 0.25
    nms.pickTop.perClass = True
    coco = yaml.safe_load((Path(libreyolo.__file__).parent / "config/datasets/coco.yaml").read_text())
    labels = list(coco["names"].values()) if isinstance(coco["names"], dict) else list(coco["names"])
    assert len(labels) == 80, labels
    nms.stringClassLabels.vector.extend(labels)
    nms_model = ct.models.MLModel(spec, skip_model_load=True)
    combined = ct.models.utils.make_pipeline(converted, nms_model)
    combined.user_defined_metadata.update({
        "brakecheck_detector_family": "rtmdet",
        "brakecheck_export_version": "N31",
        "source_license": "Apache-2.0",
        "source_model": f"LibreRTMDet{size}",
        "source_class_count": "80",
    })
    combined.short_description = f"Apache RTMDet-{size}: RGB/BGR preprocessing, normalized boxes and COCO NMS"
    output.parent.mkdir(parents=True, exist_ok=True)
    combined.save(str(output))
    files = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in output.rglob("*") if p.is_file()}
    (output.parent / f"{output.stem}-provenance.json").write_text(json.dumps({
        "source": f"https://huggingface.co/LibreYOLO/LibreRTMDet{size}",
        "license": "Apache-2.0", "input": "RGB 640x640, upstream BGR normalization",
        "source_weights_sha256": source_hash,
        "torch": torch.__version__, "coremltools": ct.__version__, "files": files,
        "validation": "PyTorch/traced parity; Apple runtime parity and phone latency pending",
    }, indent=2))
    print(f"Exported {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=("t", "s"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.size, args.output)
