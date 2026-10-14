"""Static Core ML and source provenance checks; does not execute Core ML."""
from pathlib import Path
import hashlib
import json
import coremltools as ct
import torch
from libreyolo import LibreYOLO
from export_rtmdet import VisionRTMDet

root = Path(__file__).resolve().parents[2]
resources = root / "ios/Sources/BrakeCheck/Resources"
expected = {"Day": ("t", "79b8a0b1e22b100e36a5591b24a1a97eae2fc3f29ad73cb4809d8c9c6cd19a52", "RTMDetTiny"),
            "Night": ("s", "e352310fc6a8a48b862afbbeaf5ede67002fcdb4a488f87633e2a913a9ccaa85", "RTMDetSmall")}

def file_references(message):
    for field, value in message.ListFields():
        if field.name == "fileName":
            yield value
        if field.type == field.TYPE_MESSAGE:
            values = (value.values() if hasattr(value, "values") else value) if field.is_repeated else (value,)
            for child in values:
                if hasattr(child, "ListFields"):
                    yield from file_references(child)

for profile, (size, digest, name) in expected.items():
    assert hashlib.sha256((root / f"weights/LibreRTMDet{size}.pt").read_bytes()).hexdigest() == digest
    card = (resources / f"ModelLicenses/{name}-MODEL-CARD.md").read_text()
    assert "license: apache-2.0" in card and digest in card
    package = resources / "DetectorModelData" / profile
    spec = ct.models.utils.load_spec(str(package))
    assert spec.WhichOneof("Type") == "pipeline"
    assert len(spec.pipeline.models) == 2
    image = spec.description.input[0]
    assert image.name == "image" and image.type.imageType.width == 640 and image.type.imageType.height == 640
    assert {o.name for o in spec.description.output} == {"confidence", "coordinates"}
    nms = spec.pipeline.models[1].nonMaximumSuppression
    labels = list(nms.stringClassLabels.vector)
    assert len(labels) == 80 and labels[2] == "car" and labels[3] == "motorcycle" and labels[5] == "bus" and labels[7] == "truck"
    assert nms.pickTop.perClass and nms.iouThreshold == .5 and nms.confidenceThreshold == .25
    assert nms.coordinatesInputFeatureName == "raw_coordinates" and nms.confidenceInputFeatureName == "raw_confidence"
    assert spec.description.metadata.userDefined["source_model"] == f"LibreRTMDet{size}"
    assert spec.description.metadata.userDefined["source_license"] == "Apache-2.0"
    for ref in file_references(spec):
        assert ref.startswith("@model_path/weights/"), ref
        assert (package / "Data/com.apple.CoreML" / ref.removeprefix("@model_path/")).is_file(), ref
    provenance = json.loads((resources / f"ModelLicenses/{name}-provenance.json").read_text())
    for relative, converted_digest in provenance["files"].items():
        assert hashlib.sha256((package / relative).read_bytes()).hexdigest() == converted_digest
    # Trace must preserve data dependence and exact BGR normalization/box
    # decoding, not only reproduce the gray warm-up frame used for export.
    torch.set_num_threads(4)
    wrapper = VisionRTMDet(LibreYOLO(f"LibreRTMDet{size}.pt", device="cpu").model).eval()
    sample = torch.full((1, 3, 640, 640), 114.0)
    traced = torch.jit.trace(wrapper, sample)
    generator = torch.Generator().manual_seed(31)
    varied = torch.rand(sample.shape, generator=generator) * 255
    with torch.no_grad():
        eager_scores, eager_boxes = wrapper(varied)
        trace_scores, trace_boxes = traced(varied)
        torch.testing.assert_close(eager_scores, trace_scores)
        torch.testing.assert_close(eager_boxes, trace_boxes)
        normalized = (varied[:, [2, 1, 0]] - wrapper.mean) / wrapper.std
        raw = wrapper.net(normalized)[0]
        torch.testing.assert_close(eager_scores, raw[:, 4:])
        torch.testing.assert_close(eager_boxes[:, :2], (raw[:, :2] + raw[:, 2:4]) / 1280)
        torch.testing.assert_close(eager_boxes[:, 2:], (raw[:, 2:4] - raw[:, :2]) / 640)
    print(f"{profile}: verified source hash/license, RGB image contract, COCO labels/NMS, graph weights and converted hashes")
