"""Print the UDID of an available iPhone Simulator: the newest iOS, preferring a Pro (not Max) model.

Runner images change their device list with each Xcode, so CI asks instead of naming a device.
"""
import json
import subprocess
import sys

listing = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "devices", "available", "-j"]))
phones = []
for runtime, devices in listing["devices"].items():
    if ".iOS-" not in runtime:
        continue
    version = tuple(int(part) for part in runtime.rsplit(".iOS-", 1)[1].split("-") if part.isdigit())
    for device in devices:
        if device.get("isAvailable", True) and device["name"].startswith("iPhone"):
            phones.append((version, device["name"], device["udid"]))
if not phones:
    sys.exit("No iPhone Simulator is available on this runner")


def rank(phone):
    version, name, _ = phone
    return version, name.endswith(" Pro"), "Max" not in name and "Plus" not in name


version, name, udid = max(phones, key=rank)
print(f"Using {name} (iOS {'.'.join(map(str, version))})", file=sys.stderr)
print(udid)
