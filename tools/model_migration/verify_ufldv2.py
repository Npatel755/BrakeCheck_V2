"""Check UFLDv2 package contracts/provenance, optionally an installed IPA.

Structural and recorded TorchScript parity checks are cross-platform. This
does not claim to execute the converted Core ML graph on a Linux/Windows host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import coremltools as ct

from export_ufldv2_n66 import EXPECTED_CHECKPOINT, EXPECTED_MANIFEST, HEADS

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_references(message):
    for field, value in message.ListFields():
        if field.name == "fileName":
            yield value
        if field.type == field.TYPE_MESSAGE:
            values = (value.values() if hasattr(value, "values") else value) if field.is_repeated else (value,)
            for child in values:
                if hasattr(child, "ListFields"):
                    yield from file_references(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, default=RESOURCES)
    parser.add_argument("--ipa", type=Path)
    args = parser.parse_args()
    resources = args.resources.resolve()
    package = resources / "UFLDv2Lane.mlpackage"
    provenance = json.loads((resources / "UFLDv2Lane-provenance.json").read_text())
    assert provenance["checkpoint_sha256"] == EXPECTED_CHECKPOINT
    assert provenance["manifest_sha256"] == EXPECTED_MANIFEST
    assert provenance["checkpoint_epoch"] == 3
    assert provenance["decoder"]["display_policy"] == "recall_budget_v4"
    assert provenance["validation"]["reserved_test_images_used"] == 0
    assert not provenance["decoder"]["experimental_decoder_promoted"]
    expected_files = {str(p.relative_to(resources)) for p in package.rglob("*") if p.is_file()}
    assert expected_files == set(provenance["files"]), "Incomplete package hash inventory"
    for relative, expected in provenance["files"].items():
        assert sha(resources / relative) == expected, relative
    spec = ct.models.utils.load_spec(str(package))
    assert spec.WhichOneof("Type") == "mlProgram"
    assert spec.specificationVersion >= 8, "Expected iOS17 MLProgram contract"
    assert len(spec.description.input) == 1
    image = spec.description.input[0]
    assert image.name == "image"
    assert image.type.imageType.width == 1600 and image.type.imageType.height == 320
    assert image.type.imageType.colorSpace == ct.proto.FeatureTypes_pb2.ImageFeatureType.RGB
    assert {feature.name for feature in spec.description.output} == set(HEADS)
    for feature in spec.description.output:
        assert list(feature.type.multiArrayType.shape) == HEADS[feature.name]
        assert feature.type.multiArrayType.dataType == ct.proto.FeatureTypes_pb2.ArrayFeatureType.FLOAT32
    metadata = dict(spec.description.metadata.userDefined)
    assert metadata["checkpoint_sha256"] == EXPECTED_CHECKPOINT
    assert metadata["checkpoint_epoch"] == "3"
    assert metadata["dataset_manifest_sha256"] == EXPECTED_MANIFEST
    references = list(file_references(spec))
    assert references, "Converted graph must reference trained weights"
    for ref in references:
        assert ref.startswith("@model_path/weights/")
        assert (package / "Data/com.apple.CoreML" / ref.removeprefix("@model_path/")).is_file()
    parity = provenance["validation"]["eager_trace_and_preprocessing_parity"]
    assert len(parity) == 8 and len({sample["image_id"] for sample in parity}) == 8
    for sample in parity:
        assert sample["split"] == "val" and set(sample["heads"]) == set(HEADS)
        for head in sample["heads"].values():
            assert head["eager_vs_trace_max_abs"] <= 1e-5
            assert head["native_loader_vs_rgb_wrapper_max_abs"] < 1e-3
    if cross_framework := provenance["validation"].get("cross_framework_cpu_parity"):
        assert cross_framework["status"] == "PASS"
        assert cross_framework["checkpoint_sha256"] == EXPECTED_CHECKPOINT
        assert cross_framework["reserved_test_images_used"] == 0
        assert len(cross_framework["samples"]) == 8
        assert not cross_framework["coreml_runtime_executed"]
    if args.ipa:
        with zipfile.ZipFile(args.ipa) as archive:
            assert archive.testzip() is None, "IPA CRC failure"
            names = archive.namelist()
            for relative, digest in provenance["files"].items():
                matches = [name for name in names if name.endswith("LaneModelData/" + relative)]
                assert len(matches) == 1, relative
                assert hashlib.sha256(archive.read(matches[0])).hexdigest() == digest, relative
            print("IPA package and provenance hashes: PASS")
    print(json.dumps({"model": "UFLDv2Lane", "structural_checks": "PASS",
        "checkpoint_sha256": EXPECTED_CHECKPOINT, "parity_validation_images": len(parity),
        "trained_weight_references": len(references), "package_files": len(expected_files),
        "coreml_runtime_parity": "Not executed: requires Apple hardware"}, indent=2))


if __name__ == "__main__":
    main()
