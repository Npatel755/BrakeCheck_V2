#!/usr/bin/env bash
# Generates the Xcode app project around the BrakeCheck package (see AppTarget/project.yml).
# Shared by ios-ipa.yml (unsigned .ipa) and ios-testflight.yml (signed upload).
# Run from ios/. Needs xcodegen (brew install xcodegen).
set -euo pipefail

python3 scripts/app_info_plist.py Info.plist AppTarget/Info.plist --release
icons=AppTarget/Entry/Assets.xcassets/AppIcon.appiconset
mkdir -p "$icons"
cp AppIcon.png "$icons/AppIcon.png"
echo '{"info":{"author":"xcode","version":1}}' > AppTarget/Entry/Assets.xcassets/Contents.json
echo '{"images":[{"filename":"AppIcon.png","idiom":"universal","platform":"ios","size":"1024x1024"}],"info":{"author":"xcode","version":1}}' > "$icons/Contents.json"
(cd AppTarget && xcodegen generate)
