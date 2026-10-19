"""Run with ``python -m brakecheck_dataset.cli`` from BrakeCheck_Dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import comma10k_plan, fetch_comma10k, license_registry, setup
from .video import extract_video


def main() -> None:
    parser = argparse.ArgumentParser(description="BrakeCheck local dataset pipeline")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("sources")
    plan = commands.add_parser("plan-comma10k")
    plan.add_argument("--count", type=int, default=100)
    fetch = commands.add_parser("fetch-comma10k")
    fetch.add_argument("--max-bytes", type=int, default=5_000_000_000)
    video = commands.add_parser("extract-video")
    video.add_argument("--source", required=True)
    video.add_argument("--video", type=Path, required=True)
    video.add_argument("--session", required=True)
    video.add_argument("--max-frames", type=int, default=10000)
    annotate = commands.add_parser("annotate-comma10k")
    annotate.add_argument("--limit", type=int, default=100)
    annotate.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    commands.add_parser("fetch-comma-model")
    commands.add_parser("review-queue")
    commands.add_parser("build-gallery")
    arguments = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    root = arguments.root.resolve()
    if arguments.command == "init":
        setup(root).close()
        print(root)
    elif arguments.command == "sources":
        print(json.dumps(license_registry(project), indent=2))
    elif arguments.command == "plan-comma10k":
        setup(root).close()
        result = comma10k_plan(project, root, arguments.count)
        print(json.dumps({key: value for key, value in result.items() if key != "pairs"}, indent=2))
    elif arguments.command == "fetch-comma10k":
        print(json.dumps(fetch_comma10k(project, root, arguments.max_bytes), indent=2))
    elif arguments.command == "extract-video":
        print(json.dumps(extract_video(project, root, arguments.source, arguments.video,
                                       arguments.session, max_frames=arguments.max_frames), indent=2))
    elif arguments.command == "annotate-comma10k":
        from .annotate import annotate_comma10k
        print(json.dumps(annotate_comma10k(project, root, arguments.limit,
                                           arguments.device), indent=2))
    elif arguments.command == "fetch-comma-model":
        from .annotate import fetch_comma_model
        print(json.dumps(fetch_comma_model(project), indent=2))
    elif arguments.command == "review-queue":
        from .review import make_review_queue
        print(json.dumps(make_review_queue(root), indent=2))
    elif arguments.command == "build-gallery":
        from .gallery import build_gallery
        print(json.dumps(build_gallery(root), indent=2))


if __name__ == "__main__":
    main()

