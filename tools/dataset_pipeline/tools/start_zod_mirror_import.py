"""Start or resume the authorized full ZOD mirror download and individual audits."""
import json
from pathlib import Path
import subprocess
import sys
import time

from filelock import FileLock
import psutil

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "BrakeCheck_Dataset/experiments/zod_mirror"


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def running(pid, module):
    try:
        process = psutil.Process(int(pid))
        return module in process.cmdline() and str(OUTPUT) in process.cmdline()
    except (psutil.Error, ValueError, TypeError):
        return False


OUTPUT.mkdir(parents=True, exist_ok=True)
with FileLock(str(OUTPUT / "launch.lock"), timeout=0):
    for kind, module, report, complete in (
        ("download", "bcdata.zod_mirror_download", "download_status.json", "download_complete"),
        ("import", "bcdata.import_zod_mirror", "import_status.json", "audit_ready"),
    ):
        state = read(OUTPUT / report)
        launch = read(OUTPUT / (kind + "_launch.json"))
        if any(running(value, module) for value in (state.get("pid"), launch.get("pid"))):
            print(kind + ": already running")
            continue
        if state.get("phase", state.get("state")) == complete:
            print(kind + ": complete")
            continue
        with (OUTPUT / (kind + ".log")).open("ab") as log:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            process = subprocess.Popen([sys.executable, "-u", "-m", module, "--output", str(OUTPUT)],
                                       cwd=ROOT / "tools/dataset_pipeline", stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=flags)
        (OUTPUT / (kind + "_launch.json")).write_text(json.dumps({"pid": process.pid, "started_at": time.time()}), encoding="utf-8")
        print(kind + ": started PID " + str(process.pid))
print("Audits: http://127.0.0.1:8765/source-audits?source=zod_mirror")
