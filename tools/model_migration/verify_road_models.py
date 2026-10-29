"""Verify bundled UFLDv2/road contracts and hashes, optionally an xtool IPA.

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
EXPECTED_CHECKPOINT = "4838156c4bd7c237d9189ff617e150f38d96f5018b48ac53e472ea7d4b23f378"
EXPECTED_MANIFEST = "7327866bf96b4247cebce245c058b4bb9a72c6a19234a489001d36ff44153ac5"
EXPECTED_TRAINING_CODE = "5b88b3123c612e62997204666b71357ed4589163ab514c15f91eaaab6234b258"
EXPECTED_OUTPUTS = {
    "UFLDv2Lane": {"loc_row": [1, 200, 72, 4], "loc_col": [1, 100, 81, 4],
                   "exist_row": [1, 2, 72, 4], "exist_col": [1, 2, 81, 4]},
    "SegFormerRoad": {"road_scores": [1, 2, 384, 640]},
}
EXPECTED_INPUTS = {"UFLDv2Lane": [1, 3, 320, 1600], "SegFormerRoad": [1, 3, 384, 640]}
RETIRED_MODELS = tuple(f"{name}.{extension}" for name in ("UFLDLane", "IntelRoad", "YOLOPLane")
                       for extension in ("mlpackage", "mlmodel", "mlmodelc"))


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_no_retired_models(names):
    for name in names:
        parts = Path(name).parts
        assert not any(retired in parts for retired in RETIRED_MODELS), f"Retired model still bundled: {name}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipa", type=Path)
    args = parser.parse_args()
    manifest = json.loads((RESOURCES / "manifest.json").read_text(encoding="utf-8"))
    records = {record["name"]: record for record in manifest["models"]}
    assert len(records) == len(manifest["models"]) == 2
    assert set(records) == set(EXPECTED_OUTPUTS), "Unexpected lane/road model inventory"
    inventory = {str(path.relative_to(RESOURCES)).replace("\\", "/") for path in RESOURCES.rglob("*") if path.is_file()}
    assert_no_retired_models(inventory)
    assert set(manifest["files"]) == inventory - {"manifest.json"}, "Manifest must hash every bundled lane/road asset"
    for relative, expected in manifest["files"].items():
        assert sha(RESOURCES / relative) == expected, relative
    provenance = json.loads((RESOURCES / "UFLDv2Lane-provenance.json").read_text(encoding="utf-8"))
    assert provenance["checkpoint_sha256"] == EXPECTED_CHECKPOINT
    assert provenance["manifest_sha256"] == EXPECTED_MANIFEST
    assert provenance["checkpoint_epoch"] == 3
    assert provenance["training_code_sha256"] == EXPECTED_TRAINING_CODE
    assert provenance["outputs"] == EXPECTED_OUTPUTS["UFLDv2Lane"]
    assert provenance["decoder"]["row_minimum_positive_anchors"] == 37
    assert provenance["decoder"]["column_minimum_positive_anchors"] == 21
    assert provenance["decoder"]["experimental_decoder_promoted"] is False
    assert provenance["decoder"]["row_slots_zero_based"] == [1, 2]
    assert provenance["decoder"]["column_slots_zero_based"] == [0, 3]
    assert len(provenance["decoder"]["row_anchors"]) == 72
    assert len(provenance["decoder"]["col_anchors"]) == 81
    assert provenance["decoder"]["display_policy"] == "recall_budget_v4"
    assert provenance["validation"]["reserved_test_images_used"] == 0
    for name, record in records.items():
        assert record["input"] == EXPECTED_INPUTS[name]
        model = ct.models.MLModel(str(RESOURCES / f"{name}.mlpackage"), skip_model_load=True)
        spec = model.get_spec()
        assert len(spec.description.input) == 1
        image = spec.description.input[0]
        assert image.name == "image"
        assert [image.type.imageType.height, image.type.imageType.width] == record["input"][-2:]
        assert image.type.imageType.colorSpace == ct.proto.FeatureTypes_pb2.ImageFeatureType.RGB
        outputs = {feature.name: list(feature.type.multiArrayType.shape) for feature in spec.description.output}
        assert len(outputs) == len(spec.description.output)
        assert outputs == EXPECTED_OUTPUTS[name], (name, outputs)
        if name == "UFLDv2Lane":
            assert record["output_shapes"] == EXPECTED_OUTPUTS[name]
            assert record["checkpoint_sha256"] == EXPECTED_CHECKPOINT
            for feature in spec.description.output:
                assert feature.type.multiArrayType.dataType == ct.proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT32
        else:
            assert set(outputs) == {record["output"]}
        metadata = dict(spec.description.metadata.userDefined)
        assert metadata["source"] == record["source"]
        assert metadata["license"] == record["license"]
        if name == "UFLDv2Lane":
            assert metadata["source"] == "https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2"
            assert metadata["checkpoint_sha256"] == EXPECTED_CHECKPOINT
            assert metadata["checkpoint_epoch"] == "3"
            assert metadata["dataset_manifest_sha256"] == EXPECTED_MANIFEST
        print(f"{name}: RGB input, named output contracts, source metadata and package hashes PASS")
    if args.ipa:
        with zipfile.ZipFile(args.ipa) as archive:
            assert archive.testzip() is None, "IPA CRC mismatch"
            names = archive.namelist()
            assert_no_retired_models(names)
            for relative, expected in manifest["files"].items():
                matches = [name for name in names if name.endswith("LaneModelData/" + relative)]
                assert len(matches) == 1, relative
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == expected, relative
            copies = [name for name in names if name.endswith("LaneModelData/manifest.json")]
            assert len(copies) == 1
            assert archive.read(copies[0]) == (RESOURCES / "manifest.json").read_bytes()
            for license_name in ("Apache-2.0.txt", "UFLD-MIT.txt", "SegFormer-NVIDIA-License.txt"):
                assert any(name.endswith("ModelLicenses/" + license_name) for name in names), license_name
            print("IPA: UFLDv2 and unchanged SegFormer packages, provenance/hashes, licenses, retired-model exclusion and CRC PASS")


if __name__ == "__main__":
    main()
