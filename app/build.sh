#!/bin/bash
# Build Speakeasy.app headlessly (no launch): swift build + bundle + embedded tracker + ad-hoc signature.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-build}"
swift build -c release --disable-sandbox
APP="$OUT/Speakeasy.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/Speakeasy" "$APP/Contents/MacOS/Speakeasy"
swiftc -O -parse-as-library -swift-version 5 ../src/tracker.swift -o "$APP/Contents/MacOS/speakeasy-track"
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
