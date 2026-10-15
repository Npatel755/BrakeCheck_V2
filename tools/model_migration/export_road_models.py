"""Export trained UFLD ResNet18/CULane and SegFormer B0/Cityscapes for iOS.

Linux conversion is supported; Core ML execution parity requires an Apple device.
Install requirements-ar.txt in a venv, then run this script. Weights are never
substituted with random initialization. The manifest records source revisions
and hashes. RGB byte normalization is embedded in both graphs.
"""
import argparse
import hashlib
import json
from pathlib import Path

import coremltools as ct
import gdown
import torch
from torchvision.models import resnet18
from transformers import SegformerForSemanticSegmentation

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"
SEGFORMER = "nvidia/segformer-b0-finetuned-cityscapes-1024-1024"
SEGFORMER_REVISION = "21b3847fae21ddee674abd31129307b6a1235bd9"
UFLD_URL = "https://drive.google.com/uc?id=1zXBRTw50WOzvUp6XKsi8Zrk3MUC3uFuq"
UFLD_SHA256 = "7cb18e128fda9f7f6be36a338dd8e7a2a948ce2a03dad878f0e651409b4a5b51"


class NormalizedRGB(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer("mean", torch.tensor([.485, .456, .406]).reshape(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([.229, .224, .225]).reshape(1, 3, 1, 1))

    def normalize(self, image):
        return (image / 255.0 - self.mean) / self.std


class UFLD(NormalizedRGB):
    """Inference architecture matching cfzd's original trained CULane model."""
    def __init__(self, checkpoint):
        super().__init__()
        self.model = resnet18(weights=None)
        self.model.fc = torch.nn.Identity()
        self.pool = torch.nn.Conv2d(512, 8, 1)
        self.cls = torch.nn.Sequential(torch.nn.Linear(1800, 2048), torch.nn.ReLU(),
                                       torch.nn.Linear(2048, 201 * 18 * 4))
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)["model"]
        weights = {k.removeprefix("module."): v for k, v in weights.items()}
        # Training-only auxiliary segmentation weights are intentionally unused.
        weights = {k: v for k, v in weights.items() if not k.startswith("aux_")}
        expected = set(self.state_dict()) - {"mean", "std"}
        if set(weights) != expected:
            raise ValueError(f"Unexpected checkpoint keys: {set(weights) ^ expected}")
        self.load_state_dict({**self.state_dict(), **weights}, strict=True)

    def forward(self, image):
        x = self.normalize(image)
        m = self.model
        x = m.maxpool(m.relu(m.bn1(m.conv1(x))))
        x = m.layer4(m.layer3(m.layer2(m.layer1(x))))
        return self.cls(self.pool(x).reshape(1, 1800)).reshape(1, 201, 18, 4)


class RoadSegFormer(NormalizedRGB):
    def __init__(self):
        super().__init__()
        self.net = SegformerForSemanticSegmentation.from_pretrained(SEGFORMER, revision=SEGFORMER_REVISION).eval()
        assert self.net.config.id2label[0] == "road"

    def forward(self, image):
        logits = self.net(self.normalize(image), return_dict=False)[0]
        probabilities = logits.softmax(dim=1)
        road = probabilities[:, :1]
        # Non-road is the sum of ALL other classes, including vehicles/sidewalk.
        scores = torch.cat((1.0 - road, road), dim=1)
        return torch.nn.functional.interpolate(scores, size=(384, 640), mode="bilinear", align_corners=False)


def export(net, shape, name, output_name, metadata):
    net.eval()
    sample = torch.rand(shape) * 255
    traced = torch.jit.trace(net, sample)
    with torch.no_grad():
        torch.testing.assert_close(net(sample), traced(sample))
    converted = ct.convert(traced, inputs=[ct.ImageType(name="image", shape=shape,
        color_layout=ct.colorlayout.RGB, scale=1.0)], outputs=[ct.TensorType(name=output_name)],
        convert_to="mlprogram", minimum_deployment_target=ct.target.iOS17,
        compute_precision=ct.precision.FLOAT16)
    converted.user_defined_metadata.update(metadata)
    converted.save(str(RESOURCES / f"{name}.mlpackage"))
    return {"name": name, "input": list(shape), "output": output_name, **metadata}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, default=Path.home() / ".cache/brakecheck/culane_18.pth")
    args = parser.parse_args()
    torch.set_num_threads(4)
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if not args.checkpoint.exists():
        gdown.download(UFLD_URL, str(args.checkpoint), quiet=False)
    if hashlib.sha256(args.checkpoint.read_bytes()).hexdigest() != UFLD_SHA256:
        raise ValueError("UFLD checkpoint differs from the verified official CULane weights")
    RESOURCES.mkdir(parents=True, exist_ok=True)
    lane = export(UFLD(args.checkpoint), (1, 3, 288, 800), "UFLDLane", "lane_logits", {
        "source": "https://github.com/cfzd/Ultra-Fast-Lane-Detection",
        "checkpoint_url": UFLD_URL, "checkpoint_sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
        "license": "MIT", "preprocessing": "RGB bytes; ImageNet normalization; stretch full frame",
        "decoder": "CULane anchors; 200 bins plus no-lane class; [1,201,18,4]"})
    road_net = RoadSegFormer()
    road = export(road_net, (1, 3, 384, 640), "SegFormerRoad", "road_scores", {
        "source": f"https://huggingface.co/{SEGFORMER}",
        "revision": road_net.net.config._commit_hash or "unknown",
        "license": "NVIDIA Source Code License for SegFormer; non-commercial research/evaluation",
        "license_url": "https://github.com/NVlabs/SegFormer/blob/master/LICENSE",
        "preprocessing": "RGB bytes; ImageNet normalization; stretch full frame",
        "decoder": "[1,2,384,640] softmax probabilities: non-road, road"})
    files = {}
    for path in sorted(RESOURCES.glob("*.mlpackage/**/*")):
        if path.is_file():
            files[str(path.relative_to(RESOURCES))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (RESOURCES / "manifest.json").write_text(json.dumps({"models": [lane, road], "files": files}, indent=2) + "\n")
    print("Exported trained road models and SHA256 manifest", flush=True)


if __name__ == "__main__":
    main()
