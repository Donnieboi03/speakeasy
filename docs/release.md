# Releasing Speakeasy.app

Build (headless, no launch):

```sh
SPEAKEASY_VERSION=0.2.0 BUNDLE_PYTHON=1 bash app/build.sh dist        # add BUNDLE_TTS=1 to bundle Kokoro (~1.5 GB)
ditto -c -k --keepParent dist/Speakeasy.app dist/Speakeasy-0.2.0.zip
```

`BUNDLE_PYTHON=1` runs `app/bundle_python.sh`: a relocatable CPython from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone) (installed with `uv`) goes to
`Contents/Resources/python`, the backend to `Contents/Resources/backend`. The app uses `~/.local/bin/speakeasy`
when it exists (developer install) and the bundled backend otherwise.

## Updates (Sparkle-ready)

`Info.plist` already carries `SUFeedURL` (default: `appcast.xml` on the latest GitHub release) and `SUPublicEDKey`.
Today "Check for Updates…" reads the latest GitHub release and opens its page when it is newer.
To switch to [Sparkle](https://sparkle-project.org) (MIT) in-place updates:

1. `generate_keys` (from the Sparkle release) once; build with `SPEAKEASY_SPARKLE_PUBLIC_KEY=<key>`.
2. Add `.package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0")` to `app/Package.swift`,
   and replace `Updater.check()` with `SPUStandardUpdaterController(...).checkForUpdates(nil)`.
3. Per release: `generate_appcast dist/` and attach `appcast.xml` plus the zip to the GitHub release.

The app needs a Developer ID signature and notarization for Gatekeeper on other Macs; `build.sh` signs ad hoc.
