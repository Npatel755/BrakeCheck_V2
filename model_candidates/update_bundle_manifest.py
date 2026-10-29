"""Record exact bundled candidate model bytes for iOS package verification."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "ios/Sources/BrakeCheck/Resources/LaneModelData"


def main():
    models = [
        {
            "name": "CommaLane",
            "input": [1, 3, 384, 512],
            "output": "lane_scores",
            "source": "https://huggingface.co/commaai/comma10k-segnet",
            "revision": "b642c614eb0737fe7befa229f47fbd0245033038",
            "checkpoint_sha256": "8208672861ad1b111dc98f3a7c54196d29875b709c7353e2dd1b7614343fb3a8",
            "license": "MIT",
            "decoder": "[1,5,384,512] logits; class 1 lane markings",
            "status": "device lane and AR validation pending",
        },
        {
            "name": "IntelRoad",
            "input": [1, 3, 384, 640],
            "output": "road_scores",
            "source": "https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001",
            "license": "Apache-2.0",
            "decoder": "[1,2,384,640] non-road, road+mark probabilities",
            "status": "device road and AR validation pending",
        },
    ]
    files = {
        str(path.relative_to(RESOURCES)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ("CommaLane.mlpackage", "IntelRoad.mlpackage")
        for path in sorted((RESOURCES / name).rglob("*")) if path.is_file()
    }
    (RESOURCES / "manifest.json").write_text(json.dumps({"models": models, "files": files}, indent=2) + "\n")


if __name__ == "__main__":
    main()
