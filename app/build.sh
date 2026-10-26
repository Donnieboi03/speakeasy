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
# The tracker is a nested background-only helper app, NOT in Contents/MacOS: an AppKit process running from the main
# app's MacOS dir registered with LaunchServices as Speakeasy itself, so `open Speakeasy.app` only activated a stray tracker.
HELPER="$APP/Contents/Helpers/SpeakeasyTracker.app"
mkdir -p "$HELPER/Contents/MacOS"
swiftc -O -parse-as-library -swift-version 5 "$TSRC" -o "$HELPER/Contents/MacOS/speakeasy-track"
cat > "$HELPER/Contents/Info.plist" <<HPL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.donnieboi03.speakeasy.tracker</string>
<key>CFBundleName</key><string>SpeakeasyTracker</string>
<key>CFBundleExecutable</key><string>speakeasy-track</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSBackgroundOnly</key><true/>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>14.0</string>
</dict></plist>
HPL
rm -f "$TSRC"
cp NOTICE "$APP/Contents/Resources/NOTICE"
# the six OFL fonts (Source Serif 4, IBM Plex Sans, IBM Plex Mono) and their licence texts, registered at launch for the process
mkdir -p "$APP/Contents/Resources/Fonts"
cp Resources/Fonts/*.otf Resources/Fonts/OFL-*.txt "$APP/Contents/Resources/Fonts/"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.donnieboi03.speakeasy</string>
<key>CFBundleName</key><string>Speakeasy</string>
<key>CFBundleExecutable</key><string>Speakeasy</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>${SPEAKEASY_VERSION:-0.2.0}</string>
<key>CFBundleVersion</key><string>$(git rev-list --count HEAD 2>/dev/null || echo 1)</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>SUFeedURL</key><string>${SPEAKEASY_APPCAST_URL:-https://github.com/Donnieboi03/speakeasy/releases/latest/download/appcast.xml}</string>
<key>SUPublicEDKey</key><string>${SPEAKEASY_SPARKLE_PUBLIC_KEY:-}</string>
<key>SUEnableAutomaticChecks</key><false/>
<key>NSAccessibilityUsageDescription</key><string>Speakeasy reads the selected text and where it is on screen.</string>
</dict></plist>
PL
[ "${BUNDLE_PYTHON:-0}" = 1 ] && bash bundle_python.sh "$APP" ${BUNDLE_TTS:+--with-tts}
# Stable code identity so macOS privacy grants (Accessibility, Screen Recording) survive rebuilds: TCC stores the app's
# designated requirement, and the default ad-hoc one is the cdhash, which changes on every build. Pin it to the bundle id
# (or sign with a real identity: SPEAKEASY_SIGN_ID="Developer ID Application: ..." / a self-signed code-signing cert).
SIGN_ID="${SPEAKEASY_SIGN_ID:--}"
REQ='designated => identifier "com.donnieboi03.speakeasy"'
codesign --force -s "$SIGN_ID" -i com.donnieboi03.speakeasy.tracker "$HELPER"
if [ "$SIGN_ID" = "-" ]; then codesign --force -s - -i com.donnieboi03.speakeasy --requirements "=$REQ" "$APP"
else codesign --force -s "$SIGN_ID" -i com.donnieboi03.speakeasy "$APP"; fi
codesign -dr - "$APP" 2>&1 | grep designated
echo "built $APP"
