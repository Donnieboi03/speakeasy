#!/bin/bash
# speakeasy installer for macOS. Safe to re-run.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${SPEAKEASY_HOME:-$HOME/.speakeasy}"
MODEL="${SPEAKEASY_MODEL:-qwen3-vl:4b-instruct}"
VMODEL="${SPEAKEASY_VISION_MODEL:-qwen3-vl:4b-instruct}"
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/logs"
cp "$SRC/speakeasy.py" "$SRC/highlight.py" "$SRC/docmap.py" "$SRC/ground.py" "$SRC/stats.py" "$SRC/pdfpage.py" "$SRC/selog.py" "$SRC/observe.py" "$SRC/speakeasy" "$HOME_DIR/bin/"
# Live focus tracker (Swift: ScreenCaptureKit + Vision OCR + AX). Optional: without it the box is static.
if [ "$(uname)" = "Darwin" ] && [ -f "$SRC/tracker.swift" ]; then
  if command -v swiftc >/dev/null; then
    # stamp the build: git sha of the source repo when known, else a hash of tracker.swift
    BUILD_ID="$(git -C "${SPEAKEASY_REPO:-$HOME/speakeasy-repo}" rev-parse --short HEAD 2>/dev/null || true)"
    BUILD_ID="${BUILD_ID:-src}-$(shasum -a 256 "$SRC/tracker.swift" | cut -c1-8)"
    TSRC="$(mktemp -t speakeasy_track_src).swift"
    sed "s/^let BUILD = \"dev\"/let BUILD = \"$BUILD_ID\"/" "$SRC/tracker.swift" > "$TSRC"
    if swiftc -O -parse-as-library -swift-version 5 "$TSRC" -o "$HOME_DIR/bin/speakeasy-track.new" 2>"$HOME_DIR/logs/tracker_build.log"; then
      mv "$HOME_DIR/bin/speakeasy-track.new" "$HOME_DIR/bin/speakeasy-track"; echo "Built live focus tracker: $HOME_DIR/bin/speakeasy-track (build $BUILD_ID)"
    else echo "WARNING: tracker build failed (see $HOME_DIR/logs/tracker_build.log); focus box will not follow scrolling."; fi
    rm -f "$TSRC" "${TSRC%.swift}"
  else echo "NOTE: swiftc not found (xcode-select --install) - focus box will not follow scrolling."; fi
fi
chmod +x "$HOME_DIR/bin/speakeasy" "$HOME_DIR/bin/speakeasy.py"
if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then LINKDIR=/usr/local/bin; else LINKDIR="$HOME/.local/bin"; mkdir -p "$LINKDIR"; fi
ln -sf "$HOME_DIR/bin/speakeasy" "$LINKDIR/speakeasy"
echo "Installed: $LINKDIR/speakeasy"
case ":$PATH:" in *":$LINKDIR:"*) ;; *) echo "NOTE: add $LINKDIR to PATH (e.g. echo 'export PATH=\"$LINKDIR:\$PATH\"' >> ~/.zshrc)";; esac
command -v python3 >/dev/null || echo "WARNING: python3 not found (install Xcode CLT: xcode-select --install)"
if command -v ollama >/dev/null; then
  if ollama list 2>/dev/null | grep -q "^$MODEL"; then echo "Ollama model $MODEL present."
  else echo "Pulling $MODEL (~2 GB)..."; ollama pull "$MODEL" || echo "WARNING: pull failed; speakeasy will use local fallback."; fi
  if ollama list 2>/dev/null | grep -q "^$VMODEL"; then echo "Vision model $VMODEL present."
  else echo "Pulling vision model $VMODEL (~3 GB)..."; ollama pull "$VMODEL" || echo "WARNING: vision pull failed; screenshots will be ignored (text-only)."; fi
else
  echo "Ollama not found. For free local AI rewriting: brew install ollama && ollama pull $MODEL"
  echo "(Without it, speakeasy uses basic local cleanup.)"
fi
# ---- Kokoro neural TTS (mlx-audio) via launchd, if its venv exists ----
TTS_VENV="$HOME_DIR/tts-venv"
CONFIG="$HOME_DIR/config"
touch "$CONFIG"
# Create/repair the Kokoro venv (Python 3.10-3.12: spaCy/misaki do not build on newer Pythons).
if [ "$(uname)" = "Darwin" ] && [ "${SPEAKEASY_SKIP_KOKORO:-0}" != 1 ]; then
  PY=""; for v in 3.12 3.11 3.10; do command -v python$v >/dev/null && { PY=$(command -v python$v); break; }; done
  if [ -n "$PY" ]; then
    [ -x "$TTS_VENV/bin/python" ] && "$TTS_VENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] > (3,12))' || { rm -rf "$TTS_VENV"; "$PY" -m venv "$TTS_VENV"; }
    "$TTS_VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    if ! "$TTS_VENV/bin/python" -c 'import mlx_audio.server, misaki.en, spacy; spacy.load("en_core_web_sm")' >/dev/null 2>&1; then
      echo "Installing Kokoro TTS deps (mlx-audio, misaki, spaCy model)..."
      "$TTS_VENV/bin/python" -m pip install -q "mlx-audio[server]" "misaki[en]" webrtcvad-wheels uvicorn fastapi python-multipart num2words \
        "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" \
        || echo "WARNING: Kokoro deps failed; falling back to macOS say."
    fi
  else echo "No python3.10-3.12 found (brew install python@3.12) - skipping Kokoro."; fi
fi
set_cfg() { grep -q "^$1=" "$CONFIG" 2>/dev/null || echo "$1=$2" >> "$CONFIG"; }
if [ -x "$TTS_VENV/bin/mlx_audio.server" ] && [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.speakeasy.tts.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME_DIR/logs" "$HOME_DIR/tts"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.speakeasy.tts</string>
  <key>ProgramArguments</key><array>
    <string>$TTS_VENV/bin/mlx_audio.server</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$HOME_DIR/tts</string>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/tts-server.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/tts-server.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/com.speakeasy.tts" 2>/dev/null || true
  sleep 2
  for t in 1 2 3; do launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && break; sleep 2; done
  launchctl print "gui/$(id -u)/com.speakeasy.tts" >/dev/null 2>&1 && echo "Kokoro TTS server installed as LaunchAgent (com.speakeasy.tts on 127.0.0.1:8000)."
  set_cfg SPEAKEASY_TTS kokoro
  set_cfg SPEAKEASY_TTS_VOICE af_heart
  set_cfg SPEAKEASY_TTS_URL http://127.0.0.1:8000/v1/audio/speech
  for i in $(seq 1 40); do curl -s --max-time 5 -o /dev/null http://127.0.0.1:8000/docs && break; sleep 3; done
  curl -s --max-time 120 -o /dev/null -X POST http://127.0.0.1:8000/v1/audio/speech -H 'Content-Type: application/json' \
    -d '{"model":"mlx-community/Kokoro-82M-bf16","input":"Ready.","voice":"af_heart","response_format":"wav"}' && echo "Kokoro model warmed up." || echo "WARNING: Kokoro warm-up failed (see $HOME_DIR/logs/tts-server.log)."
  echo "Default TTS set to kokoro in $CONFIG (falls back to say if the server is down)."
else
  echo "Kokoro venv not found at $TTS_VENV; using macOS say. (Optional: python3 -m venv $TTS_VENV && $TTS_VENV/bin/pip install mlx-audio, then re-run.)"
fi
# Document-aware focus (PDF parsing): PyMuPDF in the Kokoro venv (AGPL-3.0; fine for local personal use)
if [ -x "$TTS_VENV/bin/python" ]; then
  "$TTS_VENV/bin/python" -c 'import pypdfium2, PIL' 2>/dev/null || "$TTS_VENV/bin/python" -m pip install -q pypdfium2 pillow >/dev/null 2>&1 \
    && echo "pypdfium2 ready for document-aware focus" || echo "NOTE: pypdfium2 not installed; PDF figure mapping off (Markdown still works)"
fi
set_cfg SPEAKEASY_NO_SELECTION_CAPTURE window
set_cfg SPEAKEASY_VISION auto
set_cfg SPEAKEASY_FOCUS on
set_cfg SPEAKEASY_FOCUS_TAGS off
set_cfg SPEAKEASY_TRACK on
set_cfg SPEAKEASY_DOC_READ local
set_cfg SPEAKEASY_GROUND auto
set_cfg SPEAKEASY_FLORENCE off
set_cfg SPEAKEASY_KEEP_ALIVE 10m
# RAM: the 4-bit (Q4_K_M) build of the model is what fits next to Kokoro on 16-18 GB Macs
if command -v ollama >/dev/null; then
  Q="$(ollama show "${SPEAKEASY_MODEL:-qwen3-vl:4b-instruct}" 2>/dev/null | awk '/quantization/{print $2}')"
  case "$Q" in Q4_K_M|"") ;; *) echo "NOTE: ${SPEAKEASY_MODEL:-qwen3-vl:4b-instruct} is $Q, not Q4_K_M; a Q4_K_M tag uses less RAM." ;; esac
fi
set_cfg SPEAKEASY_TRACK_FPS 5
# old default (3) -> 5 scans/s while speaking
if grep -qx 'SPEAKEASY_TRACK_FPS=3' "$CONFIG" 2>/dev/null; then sed -i '' 's/^SPEAKEASY_TRACK_FPS=3$/SPEAKEASY_TRACK_FPS=5/' "$CONFIG"; fi
set_cfg SPEAKEASY_FOCUS_COLOR "#3B82F6"
set_cfg SPEAKEASY_FOCUS_OPACITY 0.22
echo "Config defaults: $CONFIG (KEY=VALUE; env vars override)."

# The menu-bar app owns the hotkey, capture and the tracker (Hammerspoon is no longer used).
APP_SRC="$(cd "$SRC/.." && pwd)/app"
if [ -d "$APP_SRC" ] && command -v swift >/dev/null; then
  if (cd "$APP_SRC" && bash build.sh "$HOME/Applications" >"$HOME_DIR/logs/app_build.log" 2>&1); then
    echo "Built $HOME/Applications/Speakeasy.app (not launched)."
  else echo "NOTE: app build failed; see $HOME_DIR/logs/app_build.log"; fi
fi
cat <<MSG

Open ~/Applications/Speakeasy.app once (it stays in the menu bar; add it to Login Items to start with your Mac).
 Hotkey: Control-Option-S reads the selection (press again to stop); change it from the menu bar icon.
 The app asks for Accessibility and Screen Recording only when you click "Grant" in its Permissions window.
 On first launch it takes the hotkey over from an older Hammerspoon speakeasy block (SPEAKEASY_HOTKEY=app); that block
 can then be deleted from ~/.hammerspoon/init.lua.
 Nothing selected? the focused window is explained (SPEAKEASY_NO_SELECTION_CAPTURE=window|none in ~/.speakeasy/config).
 Pluggable TTS: SPEAKEASY_TTS=cmd SPEAKEASY_TTS_CMD='your-tts --rate {rate} {text}' (text piped to stdin if no {text}).
 Logs: $HOME_DIR/logs/runs.jsonl   Stats: speakeasy stats --json   Test: echo '| a | b |' | speakeasy rewrite
MSG
