"""Stop only this workspace's review server, including Windows venv redirectors."""
from pathlib import Path
import argparse
import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    script = str((root / "review.py").resolve()).lower()
    targets = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
            if process.info["name"].lower() not in ("python.exe", "pythonw.exe"):
                continue
            if not any(str(Path(value).resolve()).lower() == script for value in command if "review.py" in value):
                continue
            if "serve" not in command:
                continue
            port = int(command[command.index("--port") + 1]) if "--port" in command else 8765
            if port == args.port:
                targets.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
    # Children terminate first. Never stop generic Python or another app server.
    for process in reversed(targets):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    if alive:
        raise SystemExit("A matched review process did not exit; no other processes were touched.")
    print(f"Stopped {len(targets)} matching review server processes on port {args.port}.")


if __name__ == "__main__":
    main()
