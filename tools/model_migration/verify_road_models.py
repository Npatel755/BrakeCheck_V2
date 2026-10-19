"""Verify bundled model contracts/hashes, optionally also an xtool IPA.

This is structural verification, not Apple Core ML execution parity.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
import coremltools as ct

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipa", type=Path)
    args = parser.parse_args()
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((RESOURCES / relative).read_bytes()).hexdigest() == expected, relative
    expected_outputs = {"UFLDLane": [1, 201, 18, 4], "SegFormerRoad": [1, 2, 384, 640]}
    for record in manifest["models"]:
        model = ct.models.MLModel(str(RESOURCES / f'{record["name"]}.mlpackage'), skip_model_load=True)
        spec = model.get_spec()
        image = spec.description.input[0]
        assert image.name == "image"
        assert [image.type.imageType.height, image.type.imageType.width] == record["input"][-2:]
        assert image.type.imageType.colorSpace == ct.proto.FeatureTypes_pb2.ImageFeatureType.RGB
        output = spec.description.output[0]
        assert output.name == record["output"]
        assert list(output.type.multiArrayType.shape) == expected_outputs[record["name"]]
        assert dict(spec.description.metadata.userDefined)["source"] == record["source"]
        assert dict(spec.description.metadata.userDefined)["license"] == record["license"]
        print(f'{record["name"]}: RGB input, output contract, source metadata and package hashes PASS')
    if args.ipa:
        with zipfile.ZipFile(args.ipa) as archive:
            assert archive.testzip() is None, "IPA CRC mismatch"
            names = archive.namelist()
            assert not any("YOLOPLane.mlmodel" in name for name in names)
            for relative, expected in manifest["files"].items():
                matches = [name for name in names if name.endswith("LaneModelData/" + relative)]
                assert len(matches) == 1, relative
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == expected, relative
            assert any(name.endswith("ModelLicenses/Apache-2.0.txt") for name in names)
            assert any(name.endswith("ModelLicenses/UFLD-MIT.txt") for name in names)
            assert any(name.endswith("ModelLicenses/SegFormer-NVIDIA-License.txt") for name in names)
            assert not any("IntelRoad.mlpackage" in name for name in names)
            print("IPA: UFLD and SegFormer packages, SHA256 contents, licenses and CRC integrity PASS")


if __name__ == "__main__":
    main()
