"""Local dataset pipeline entry point. Training is deliberately outside this flow."""
from pathlib import Path
import argparse
import json
import logging
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser(description="BrakeCheck UFLDv2 dataset review pipeline")
    parser.add_argument("command", choices=["prepare", "serve", "status", "export", "backup"], nargs="?", default="serve")
    parser.add_argument("--data-root")
    parser.add_argument("--archive")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from bcdata.local import local_settings
    settings = local_settings(args.data_root)
    if args.command == "prepare":
        from bcdata.bootstrap import prepare
        print(json.dumps(prepare(args.data_root, args.archive, args.offline), indent=2))
    elif args.command == "serve":
        if not settings.db_path.exists():
            from bcdata.bootstrap import prepare
            prepare(args.data_root, args.archive, args.offline)
        import uvicorn
        from bcdata.review_server import create_app
        uvicorn.run(create_app(settings, port=args.port), host="0.0.0.0", port=args.port, log_level="info")
    elif args.command == "status":
        from bcdata.review_store import ReviewStore
        print(json.dumps(ReviewStore(settings).stats(), indent=2))
    elif args.command == "export":
        from bcdata.review_export import export_reviewed
        print(json.dumps(export_reviewed(settings), indent=2))
    elif args.command == "backup":
        from bcdata.review_store import ReviewStore
        print(ReviewStore(settings).backup())


if __name__ == "__main__":
    main()
