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


def expected_files():
    for folder, record_path in RECORDS:
        record = json.loads((RESOURCES / record_path).read_text())
        for relative, digest in record["files"].items():
            yield folder + "/" + relative, digest
    # The viewpoint classifier embeds its weights in one legacy model file.
    path = RESOURCES / "ViewpointModelData/VehicleViewpoint.mlmodel"
    assert path.stat().st_size > 100_000_000, "Viewpoint classifier missing/truncated"
    yield "ViewpointModelData/VehicleViewpoint.mlmodel", "ef668af0e2358f206b3c4030557d9ba10be61ed08390819ec032c6dbbbab7aff"


def verify(ipa=None):
    expected = dict(expected_files())
    for relative, digest in expected.items():
        path = RESOURCES / relative
        assert path.is_file(), f"Missing model asset: {relative}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"Model hash mismatch: {relative}"
    for folder, _ in RECORDS[:2]:
        manifest = json.loads((RESOURCES / folder / "Manifest.json").read_text())
        for entry in manifest["itemInfoEntries"].values():
            assert (RESOURCES / folder / "Data" / entry["path"]).exists(), entry["path"]
    print("ALL models: detector day/night, UFLD, SegFormer, viewpoint weights and SHA256 PASS")
    if ipa:
        with zipfile.ZipFile(ipa) as archive:
            assert archive.testzip() is None, "IPA CRC failure"
            for relative, digest in expected.items():
                matches = [name for name in archive.namelist() if name.endswith("/" + relative)]
                assert len(matches) == 1, f"IPA missing/duplicate asset: {relative}"
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == digest, relative
        print("IPA: every model weight present, matching SHA256 and CRC PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipa", type=Path)
    verify(parser.parse_args().ipa)
