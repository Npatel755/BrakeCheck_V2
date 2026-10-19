"""Record the Intel road model as the bundled road segmentation candidate."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"
MANIFEST = RESOURCES / "manifest.json"


def main():
    intel = RESOURCES / "IntelRoad.mlpackage"
    if not intel.is_dir():
        raise ValueError(f"Missing {intel}")
    if (RESOURCES / "SegFormerRoad.mlpackage").exists():
        raise ValueError("Remove the old road package from app resources first")
    manifest = json.loads(MANIFEST.read_text())
    manifest["models"] = [item for item in manifest["models"] if item["name"] != "SegFormerRoad"]
    manifest["models"].append({
        "name": "IntelRoad",
        "input": [1, 3, 384, 640],
        "output": "road_scores",
        "source": "https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001",
        "license": "Apache-2.0",
        "license_url": "https://github.com/openvinotoolkit/open_model_zoo/blob/master/LICENSE",
        "preprocessing": "RGB bytes to BGR; resize to 896x512",
        "decoder": "[1,2,384,640] probabilities: non-road, road+mark",
        "status": "test candidate; device accuracy and Core ML execution pending",
    })
    manifest["files"] = {
        str(path.relative_to(RESOURCES)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for package in ("UFLDLane.mlpackage", "IntelRoad.mlpackage")
        for path in sorted((RESOURCES / package).rglob("*")) if path.is_file()
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
