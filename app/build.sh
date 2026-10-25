#!/bin/bash
# Build Speakeasy.app headlessly (no launch): swift build + bundle + embedded tracker + ad-hoc signature.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-build}"
swift build -c release --disable-sandbox
APP="$OUT/Speakeasy.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/Speakeasy" "$APP/Contents/MacOS/Speakeasy"
# the embedded tracker is src/tracker.swift with the same BUILD stamp install.sh uses (git sha + source hash)
BUILD_ID="$(git rev-parse --short HEAD 2>/dev/null || echo src)-$(shasum -a 256 ../src/tracker.swift | cut -c1-8)"
TSRC="$(mktemp -t speakeasy_track_src).swift"
sed "s/^let BUILD = \"dev\"/let BUILD = \"$BUILD_ID\"/" ../src/tracker.swift > "$TSRC"
swiftc -O -parse-as-library -swift-version 5 "$TSRC" -o "$APP/Contents/MacOS/speakeasy-track"
rm -f "$TSRC"
cp NOTICE "$APP/Contents/Resources/NOTICE"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.donnieboi03.speakeasy</string>
<key>CFBundleName</key><string>Speakeasy</string>
<key>CFBundleExecutable</key><string>Speakeasy</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSAccessibilityUsageDescription</key><string>Speakeasy reads the selected text and where it is on screen.</string>
</dict></plist>
PL
codesign --force --deep -s - "$APP"
echo "built $APP"
