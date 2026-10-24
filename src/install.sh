#!/bin/bash
# speakeasy installer for macOS. Safe to re-run.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${SPEAKEASY_HOME:-$HOME/.speakeasy}"
MODEL="${SPEAKEASY_MODEL:-qwen2.5:3b-instruct}"
VMODEL="${SPEAKEASY_VISION_MODEL:-qwen2.5vl:3b}"
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/logs"
cp "$SRC/speakeasy.py" "$SRC/highlight.py" "$SRC/speakeasy" "$HOME_DIR/bin/"
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
set_cfg SPEAKEASY_NO_SELECTION_CAPTURE window
echo "Config defaults: $CONFIG (KEY=VALUE; env vars override)."

cat <<MSG

Bind a hotkey (pick one):
 A) Shortcuts app: New Shortcut -> add 'Run Shell Script' with:
      $LINKDIR/speakeasy selection
    Shortcut details -> 'Use as Quick Action' + 'Services Menu'.
    Then System Settings -> Keyboard -> Keyboard Shortcuts -> Services -> assign a key.
 B) Hammerspoon (recommended; also captures a screenshot of the selection for figures/diagrams).
    Paste into ~/.hammerspoon/init.lua (replacing any older speakeasy block), then Reload Config:

-- speakeasy: ctrl+alt+S reads the selection (+ a tight screenshot of it); press again to stop
-- CAPTURE: "auto" (exact AX bounds -> highlight-colour detection -> drag-to-select -> text only),
--          "select" (always drag a box; Esc = text only), "mouse" (900x600 around cursor), "off" (never screenshot)
local CAPTURE = os.getenv("SPEAKEASY_CAPTURE") or "auto"
local SPEAKEASY = "$LINKDIR/speakeasy"
local SHOT = "/tmp/speakeasy_cap_sel.png"
local WINSHOT = "/tmp/speakeasy_cap_win.png"

-- read a key from ~/.speakeasy/config (Hammerspoon does not see shell env vars)
local function cfg(key, default)
  local v = os.getenv(key)
  if v and v ~= "" then return v end
  local f = io.open(os.getenv("HOME") .. "/.speakeasy/config", "r")
  if f then
    for line in f:lines() do
      local k, val = line:match("^%s*([%w_]+)%s*=%s*(.-)%s*$")
      if k == key and val ~= "" then f:close(); return (val:gsub("^[\"']", ""):gsub("[\"']$", "")) end
    end
    f:close()
  end
  return default
end
-- What to capture when NOTHING is selected: window (default) | screen | none
local NO_SEL = cfg("SPEAKEASY_NO_SELECTION_CAPTURE", "window")

-- Is any text selected? Copy with Cmd-C and see if the pasteboard changed; restore it afterwards.
local function hasTextSelection()
  local saved = hs.pasteboard.readAllData()
  local before = hs.pasteboard.changeCount()
  hs.eventtap.keyStroke({ "cmd" }, "c", 0)
  local changed = false
  for _ = 1, 8 do
    hs.timer.usleep(25000)
    if hs.pasteboard.changeCount() ~= before then changed = true; break end
  end
  local txt = changed and (hs.pasteboard.getContents() or "") or ""
  if changed and saved then hs.pasteboard.writeAllData(saved) end
  return changed and txt:match("%S") ~= nil
end

local function clamp(r)
  local sf = hs.mouse.getCurrentScreen():fullFrame()
  local w, h = math.min(r.w, sf.w), math.min(r.h, sf.h)
  local x = math.max(sf.x, math.min(r.x, sf.x + sf.w - w))
  local y = math.max(sf.y, math.min(r.y, sf.y + sf.h - h))
  return { x = math.floor(x), y = math.floor(y), w = math.floor(w), h = math.floor(h) }
end

local function goodRect(b) return b and b.w and b.h and b.w > 4 and b.h > 4 end
local function pad(b) return { x = b.x - 16, y = b.y - 16, w = b.w + 32, h = b.h + 32 } end

-- Try every AX route to the selection bounds on one element.
local function boundsOf(el)
  local ok, r = pcall(function()
    local range = el:attributeValue("AXSelectedTextRange")
    if range and (range.length or 0) > 0 then
      local b = el:parameterizedAttributeValue("AXBoundsForRange", range)
      if goodRect(b) then return b end
    end
    local tm = el:attributeValue("AXSelectedTextMarkerRange")          -- WebKit / Chromium
    if tm then
      local b = el:parameterizedAttributeValue("AXBoundsForTextMarkerRange", tm)
      if goodRect(b) then return b end
    end
  end)
  return ok and r or nil
end

local function axSelectionRect()
  local sys = hs.axuielement.systemWideElement()
  local el = sys:attributeValue("AXFocusedUIElement")
  if not el then return nil end
  local b = boundsOf(el)
  if b then return pad(b) end
  -- walk children (depth 4, max 300 nodes) looking for an element with a selection
  local queue, seen = { { el, 0 } }, 0
  while #queue > 0 and seen < 300 do
    local item = table.remove(queue, 1); seen = seen + 1
    local node, depth = item[1], item[2]
    if node ~= el then
      b = boundsOf(node)
      if b then return pad(b) end
    end
    if depth < 4 then
      local kids = node:attributeValue("AXChildren") or {}
      for _, k in ipairs(kids) do table.insert(queue, { k, depth + 1 }) end
    end
  end
  return nil
end

local function run(args, method, rect, t0)
  table.insert(args, "--capture-method"); table.insert(args, method)
  if rect then table.insert(args, "--capture-rect"); table.insert(args, string.format("%d,%d,%d,%d", rect.x, rect.y, rect.w, rect.h)) end
  table.insert(args, "--capture-ms"); table.insert(args, tostring(math.floor((hs.timer.secondsSinceEpoch() - t0) * 1000)))
  hs.task.new(SPEAKEASY, nil, args):start()
end

-- Screenshot the focused window, then crop to the bounding box of all highlight-coloured pixels.
local function highlightCapture()
  local win = hs.window.focusedWindow()
  if not win then return nil end
  os.remove(WINSHOT); os.remove(SHOT)
  hs.execute(string.format("/usr/sbin/screencapture -x -o -l %d %s", win:id(), WINSHOT))
  if not hs.fs.attributes(WINSHOT) then return nil end
  local out = hs.execute(string.format("'%s' highlight %s %s", SPEAKEASY, WINSHOT, SHOT)) or ""
  os.remove(WINSHOT)
  local x, y, w, h = out:match("(%d+),(%d+),(%d+),(%d+)")
  if not x or not hs.fs.attributes(SHOT) then return nil end
  local f = win:frame()
  local scale = (win:screen():currentMode() or {}).scale or 2
  return { x = f.x + x / scale, y = f.y + y / scale, w = w / scale, h = h / scale }
end

local function interactive(t0)
  os.remove(SHOT)
  hs.task.new("/usr/sbin/screencapture", function()
    if hs.fs.attributes(SHOT) then run({ "selection", "--image", SHOT }, "select", nil, t0)
    else run({ "selection" }, "none", nil, t0) end      -- Esc: text only
  end, { "-i", "-x", SHOT }):start()
end

hs.hotkey.bind({ "ctrl", "alt" }, "S", function()
  local pidf = os.getenv("HOME") .. "/.speakeasy/speak.pid"
  if hs.fs.attributes(pidf) then hs.task.new(SPEAKEASY, nil, { "stop" }):start(); return end
  local t0 = hs.timer.secondsSinceEpoch()
  if CAPTURE == "off" then return run({ "selection" }, "none", nil, t0) end
  if CAPTURE == "select" then return interactive(t0) end
  if CAPTURE == "mouse" then
    local p = hs.mouse.absolutePosition()
    local r = clamp({ x = p.x - 450, y = p.y - 300, w = 900, h = 600 })
    os.remove(SHOT)
    hs.execute(string.format("/usr/sbin/screencapture -x -R %d,%d,%d,%d %s", r.x, r.y, r.w, r.h, SHOT))
    if hs.fs.attributes(SHOT) then return run({ "selection", "--image", SHOT }, "mouse", r, t0) end
    return run({ "selection" }, "none", nil, t0)
  end
  -- auto
  local r = axSelectionRect()
  if r then
    r = clamp(r); os.remove(SHOT)
    hs.execute(string.format("/usr/sbin/screencapture -x -R %d,%d,%d,%d %s", r.x, r.y, r.w, r.h, SHOT))
    if hs.fs.attributes(SHOT) then return run({ "selection", "--image", SHOT }, "bounds", r, t0) end
  end
  local hr = highlightCapture()
  if hr then return run({ "selection", "--image", SHOT }, "highlight", hr, t0) end
  if hasTextSelection() then return interactive(t0) end   -- text selected but not locatable: drag a box (Esc = text only)
  -- nothing selected: explain the focused window (or screen) per SPEAKEASY_NO_SELECTION_CAPTURE
  os.remove(SHOT)
  if NO_SEL == "window" then
    local win = hs.window.focusedWindow()
    if win then
      hs.execute(string.format("/usr/sbin/screencapture -x -o -l %d %s", win:id(), SHOT))
      local f = win:frame()
      if hs.fs.attributes(SHOT) then
        return run({ "selection", "--image", SHOT }, "no-selection-window", { x = f.x, y = f.y, w = f.w, h = f.h }, t0)
      end
    end
  elseif NO_SEL == "screen" then
    local f = hs.mouse.getCurrentScreen():fullFrame()
    hs.execute(string.format("/usr/sbin/screencapture -x -R %d,%d,%d,%d %s", f.x, f.y, f.w, f.h, SHOT))
    if hs.fs.attributes(SHOT) then
      return run({ "selection", "--image", SHOT }, "no-selection-screen", { x = f.x, y = f.y, w = f.w, h = f.h }, t0)
    end
  end
  run({ "selection" }, "none", nil, t0)
end)
-- end speakeasy

    Hammerspoon needs Accessibility AND Screen Recording permission (System Settings > Privacy & Security).
    Nothing selected? auto mode explains the focused window. Set SPEAKEASY_NO_SELECTION_CAPTURE=window|screen|none
    in ~/.speakeasy/config (default window).
    Capture modes: set CAPTURE in the block (auto|select|mouse|off). Last screenshot is kept at ~/.speakeasy/last_capture.png
    (SPEAKEASY_KEEP_CAPTURE=0 deletes it after use). Inspect runs with: speakeasy last ; speakeasy stats
    Pluggable TTS: SPEAKEASY_TTS=cmd SPEAKEASY_TTS_CMD='your-tts --rate {rate} {text}' (text piped to stdin if no {text}).
 Press the key again while speaking to stop. Grant Accessibility permission
 to Shortcuts/Hammerspoon (needed to simulate Cmd-C).
 Logs: $HOME_DIR/logs/runs.jsonl   Test: echo '| a | b |' | speakeasy rewrite
MSG
