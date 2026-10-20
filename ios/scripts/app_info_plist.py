"""Write the Info.plist for an Xcode app build from ios/Info.plist.

xtool adds the bundle keys itself, so ios/Info.plist doesn't have them. An Xcode
app target needs them, so this script adds them and keeps every other key as it is.

Usage: python3 app_info_plist.py Info.plist AppTarget/Info.plist
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    info = plistlib.loads(args.source.read_bytes())
    for key, value in BUNDLE_KEYS.items():
        info.setdefault(key, value)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(plistlib.dumps(info))
    print(f"Wrote {args.output} ({len(info)} keys)")


if __name__ == "__main__":
    main()
