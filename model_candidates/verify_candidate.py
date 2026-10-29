"""Verify converted model contracts, hashes and optional packaged IPA."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import zipfile

import coremltools as ct


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"
CONTRACTS = {
    "CommaLane": (512, 384, "lane_scores", [1, 5, 384, 512]),
    "IntelRoad": (640, 384, "road_scores", [1, 2, 384, 640]),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipa", type=Path)
    args = parser.parse_args()
    manifest = json.loads((RESOURCE / "manifest.json").read_text())
    assert {entry["name"] for entry in manifest["models"]} == set(CONTRACTS)
    for name, (width, height, output, shape) in CONTRACTS.items():
        package = RESOURCE / f"{name}.mlpackage"
        assert package.is_dir()
        spec = ct.models.MLModel(str(package), skip_model_load=True).get_spec()
        assert spec.description.input[0].name == "image"
        assert spec.description.input[0].type.imageType.width == width
        assert spec.description.input[0].type.imageType.height == height
        assert spec.description.output[0].name == output
        assert list(spec.description.output[0].type.multiArrayType.shape) == shape
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((RESOURCE / relative).read_bytes()).hexdigest() == digest, relative
    assert not (RESOURCE / "UFLDLane.mlpackage").exists()
    assert not (RESOURCE / "SegFormerRoad.mlpackage").exists()
    print("Core ML contracts, resource hashes and candidate-only resources PASS")
    if args.ipa:
        with zipfile.ZipFile(args.ipa) as archive:
            names = archive.namelist()
            assert archive.testzip() is None
            infos = [name for name in names if name.startswith("Payload/")
                     and name.count("/") == 2 and name.endswith(".app/Info.plist")]
            assert infos
            info = plistlib.loads(archive.read(infos[0]))
            assert info["CFBundleVersion"] == "53"
            for relative, digest in manifest["files"].items():
                matches = [name for name in names if name.endswith("LaneModelData/" + relative)]
                assert len(matches) == 1, relative
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == digest, relative
            assert not any("UFLDLane.mlpackage" in name or "SegFormerRoad.mlpackage" in name for name in names)
            assert any(name.endswith("ModelLicenses/Comma10k-MIT.txt") for name in names)
            assert any(name.endswith("ModelLicenses/Apache-2.0.txt") for name in names)
        print("IPA N53 candidate weights, licenses, hashes and CRC PASS")


if __name__ == "__main__":
    main()
