"""Convert a locally trained, commercially cleared Mask2Former road model.

This deliberately accepts only local checkpoints. The public Mask2Former model
zoo weights are CC BY-NC and are not suitable for a commercial BrakeCheck IPA.
The resulting package is staged outside the app; device validation and a
separate promotion step are required before replacing SegFormerRoad.
"""

import argparse
import hashlib
import json
from pathlib import Path

import coremltools as ct
import torch
import torch.nn.functional as F
from transformers import Mask2FormerForUniversalSegmentation


HEIGHT, WIDTH = 384, 640
REQUIRED_PROVENANCE = (
    "weights_source",
    "weights_license",
    "training_data_source",
    "training_data_license",
)


def checked_provenance(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    for field in REQUIRED_PROVENANCE:
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Missing model provenance: {field}")
    if record.get("commercial_use_confirmed") is not True:
        raise ValueError("Commercial-use rights for the weights and training data must be confirmed")
    return record


class RoadMask(torch.nn.Module):
    """Collapse semantic Mask2Former queries into the existing binary mask contract."""

    def __init__(self, model: Mask2FormerForUniversalSegmentation, road_class: int):
        super().__init__()
        self.model = model.eval()
        self.road_class = road_class
        self.register_buffer("mean", torch.tensor([.485, .456, .406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([.229, .224, .225]).view(1, 3, 1, 1))

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        image = (image / 255.0 - self.mean) / self.std
        result = self.model(image, return_dict=False)
        class_logits, mask_logits = result[0], result[1]
        # The final class is Mask2Former's no-object class.
        class_prob = class_logits.softmax(dim=-1)[..., self.road_class]
        mask_prob = mask_logits.sigmoid()
        road = (class_prob[:, :, None, None] * mask_prob).sum(dim=1, keepdim=True)
        road = F.interpolate(road, size=(HEIGHT, WIDTH), mode="bilinear", align_corners=False)
        road = road.clamp(0, 1)
        return torch.cat((1 - road, road), dim=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True,
                        help="Local Hugging Face Mask2Former checkpoint directory")
    parser.add_argument("--provenance", type=Path, required=True,
                        help="JSON recording commercial rights for weights and all training data")
    parser.add_argument("--output", type=Path, required=True,
                        help="Staging directory; never point directly into iOS app resources")
    args = parser.parse_args()
    checkpoint = args.checkpoint.resolve(strict=True)
    if not checkpoint.is_dir() or not (checkpoint / "config.json").is_file():
        raise ValueError("Checkpoint must be a local directory containing config.json")
    if not any((checkpoint / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")):
        raise ValueError("Checkpoint weights are missing; random initialization is prohibited")
    provenance = checked_provenance(args.provenance)
    output = args.output.resolve()
    app_resources = (Path(__file__).resolve().parents[2] /
                     "ios/Sources/BrakeCheck/Resources/LaneModelData").resolve()
    if output == app_resources or app_resources in output.parents:
        raise ValueError("Stage the model outside app resources until iPhone validation passes")
    output.mkdir(parents=True, exist_ok=True)

    model = Mask2FormerForUniversalSegmentation.from_pretrained(
        str(checkpoint), local_files_only=True, use_safetensors=(checkpoint / "model.safetensors").is_file()
    ).eval()
    labels = {int(k): str(v).strip().lower() for k, v in model.config.id2label.items()}
    road_ids = [index for index, label in labels.items() if label in ("road", "ego road", "ego_road")]
    if len(road_ids) != 1:
        raise ValueError(f"Expected exactly one road class in id2label, found {road_ids}")
    wrapped = RoadMask(model, road_ids[0]).eval()
    example = torch.rand(1, 3, HEIGHT, WIDTH) * 255
    with torch.no_grad():
        expected = wrapped(example)
        if expected.shape != (1, 2, HEIGHT, WIDTH) or not torch.isfinite(expected).all():
            raise ValueError("Road model does not produce finite [1,2,384,640] scores")
        traced = torch.jit.trace(wrapped, example, strict=False)
        torch.testing.assert_close(traced(example), expected, atol=1e-3, rtol=1e-3)
    converted = ct.convert(
        traced,
        inputs=[ct.ImageType(name="image", shape=example.shape, color_layout=ct.colorlayout.RGB, scale=1.0)],
        outputs=[ct.TensorType(name="road_scores")],
        convert_to="mlprogram", minimum_deployment_target=ct.target.iOS17,
        compute_precision=ct.precision.FLOAT16,
    )
    converted.user_defined_metadata.update({
        "source": provenance["weights_source"],
        "license": provenance["weights_license"],
        "training_data_source": provenance["training_data_source"],
        "training_data_license": provenance["training_data_license"],
        "preprocessing": "RGB bytes; ImageNet normalization; stretch full frame",
        "decoder": "[1,2,384,640] non-road, road probabilities",
    })
    package = output / "Mask2FormerRoad.mlpackage"
    if package.exists():
        raise FileExistsError(f"Remove or choose another staging directory: {package}")
    converted.save(str(package))
    files = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in package.rglob("*") if path.is_file()}
    (output / "provenance.json").write_text(json.dumps({**provenance, "files": files}, indent=2) + "\n")
    print(f"Staged {package}; validate on iPhone before bundling")


if __name__ == "__main__":
    main()
