"""SHA256 checks for ALL bundled weights; optionally inspect the finished IPA.

Standard library only. This verifies packaging, not Apple runtime execution.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources"
RECORDS = (
    ("DetectorModelData/Day", "ModelLicenses/RTMDetTiny-provenance.json"),
    ("DetectorModelData/Night", "ModelLicenses/RTMDetSmall-provenance.json"),
    ("LaneModelData", "LaneModelData/manifest.json"),
)
RETIRED_MODELS = tuple(f"{name}.{extension}" for name in ("UFLDLane", "SegFormerRoad", "YOLOPLane")
                       for extension in ("mlpackage", "mlmodel", "mlmodelc"))
EXPECTED_CHECKPOINT = "4838156c4bd7c237d9189ff617e150f38d96f5018b48ac53e472ea7d4b23f378"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_no_retired_models(names):
    for name in names:
        assert not any(retired in Path(name).parts for retired in RETIRED_MODELS), f"Retired model still bundled: {name}"


def expected_files():
    for folder, record_path in RECORDS:
        record = json.loads((RESOURCES / record_path).read_text(encoding="utf-8"))
        yield record_path, sha(RESOURCES / record_path)
        if folder == "LaneModelData":
            assert {model["name"] for model in record["models"]} == {"UFLDv2Lane", "IntelRoad"}
            road = next(model for model in record["models"] if model["name"] == "IntelRoad")
            assert road["license"] == "Apache-2.0", "Bundled road model must stay commercially licensed"
            inventory = {str(path.relative_to(RESOURCES / folder)).replace("\\", "/") for path in (RESOURCES / folder).rglob("*") if path.is_file()}
            assert set(record["files"]) == inventory - {"manifest.json"}, "Lane manifest file inventory is incomplete"
            lane = next(model for model in record["models"] if model["name"] == "UFLDv2Lane")
            assert lane["checkpoint_sha256"] == EXPECTED_CHECKPOINT
            provenance = json.loads((RESOURCES / folder / "UFLDv2Lane-provenance.json").read_text(encoding="utf-8"))
            assert provenance["checkpoint_sha256"] == EXPECTED_CHECKPOINT
            assert provenance["checkpoint_epoch"] == 3
        for relative, digest in record["files"].items():
            yield folder + "/" + relative, digest
    # The viewpoint classifier embeds its weights in one legacy model file.
    path = RESOURCES / "ViewpointModelData/VehicleViewpoint.mlmodel"
    assert path.stat().st_size > 100_000_000, "Viewpoint classifier missing/truncated"
    yield "ViewpointModelData/VehicleViewpoint.mlmodel", "ef668af0e2358f206b3c4030557d9ba10be61ed08390819ec032c6dbbbab7aff"


def verify(ipa=None):
    # Match the .copy resource roots in ios/Package.swift.
    bundled_roots = ("DetectorModelData", "LaneModelData", "ViewpointModelData", "ModelLicenses", "Fonts")
    for folder in bundled_roots:
        assert_no_retired_models(str(path.relative_to(RESOURCES)) for path in (RESOURCES / folder).rglob("*"))
    assert not (RESOURCES / "YOLOModelData").exists(), "AGPL Ultralytics resources must not return to the package"
    assert not any("NVIDIA" in path.name or "SegFormer" in path.name for path in (RESOURCES / "ModelLicenses").iterdir())
    expected = dict(expected_files())
    for relative, digest in expected.items():
        path = RESOURCES / relative
        assert path.is_file(), f"Missing model asset: {relative}"
        assert sha(path) == digest, f"Model hash mismatch: {relative}"
    for folder, _ in RECORDS[:2]:
        manifest = json.loads((RESOURCES / folder / "Manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["itemInfoEntries"].values():
            assert (RESOURCES / folder / "Data" / entry["path"]).exists(), entry["path"]
    print("ALL models: detector day/night, trained UFLDv2, Apache-2.0 Intel road, viewpoint weights/provenance and SHA256 PASS")
    if ipa:
        with zipfile.ZipFile(ipa) as archive:
            assert archive.testzip() is None, "IPA CRC failure"
            names = archive.namelist()
            assert_no_retired_models(names)
            for relative, digest in expected.items():
                matches = [name for name in names if name.endswith("/" + relative)]
                assert len(matches) == 1, f"IPA missing/duplicate asset: {relative}"
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == digest, relative
        print("IPA: every model weight and provenance record present, matching SHA256, retired-model exclusion and CRC PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipa", type=Path)
    verify(parser.parse_args().ipa)
