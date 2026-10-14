"""Write the Info.plist for an Xcode app build from ios/Info.plist.

xtool adds the bundle keys itself, so ios/Info.plist doesn't have them. An Xcode
app target needs them, so this script adds them. With --release it also removes the
keys only the hackathon backend needs (Bonjour and local-network access), matching
AppScope in Release builds.

Usage: python3 app_info_plist.py ../Info.plist AppTarget/Info.plist [--release]
"""
import argparse
import plistlib
from pathlib import Path

BUNDLE_KEYS = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleExecutable": "$(EXECUTABLE_NAME)",
    "CFBundleIdentifier": "$(PRODUCT_BUNDLE_IDENTIFIER)",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": "$(PRODUCT_NAME)",
    "CFBundlePackageType": "APPL",
    "LSRequiresIPhoneOS": True,
}

# Only the backend pairing uses these.
BACKEND_ONLY = ("NSBonjourServices", "NSLocalNetworkUsageDescription", "NSAppTransportSecurity")

# The check-in reply recorder stays in the binary, so the purpose string stays too.
RELEASE_MICROPHONE = "BrakeCheck asks for the microphone only to record a spoken reply during a crash check-in."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()

    info = plistlib.loads(args.source.read_bytes())
    for key, value in BUNDLE_KEYS.items():
        info.setdefault(key, value)
    if args.release:
        for key in BACKEND_ONLY:
            info.pop(key, None)
        info["NSMicrophoneUsageDescription"] = RELEASE_MICROPHONE
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(plistlib.dumps(info))
    print(f"Wrote {args.output} ({'release' if args.release else 'debug'}, {len(info)} keys)")


if __name__ == "__main__":
    main()
