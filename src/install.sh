#!/bin/bash
# speakeasy installer for macOS. Safe to re-run.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${SPEAKEASY_HOME:-$HOME/.speakeasy}"
MODEL="${SPEAKEASY_MODEL:-qwen3-vl:4b-instruct}"
VMODEL="${SPEAKEASY_VISION_MODEL:-qwen3-vl:4b-instruct}"
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/logs"
cp "$SRC/speakeasy.py" "$SRC/highlight.py" "$SRC/docmap.py" "$SRC/ground.py" "$SRC/speakeasy" "$HOME_DIR/bin/"
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
  "$TTS_VENV/bin/python" -c 'import pymupdf, PIL' 2>/dev/null || "$TTS_VENV/bin/python" -m pip install -q pymupdf pillow >/dev/null 2>&1 \
    && echo "PyMuPDF ready for document-aware focus" || echo "NOTE: PyMuPDF not installed; PDF figure mapping off (Markdown still works)"
fi
set_cfg SPEAKEASY_NO_SELECTION_CAPTURE window
set_cfg SPEAKEASY_VISION auto
set_cfg SPEAKEASY_FOCUS on
set_cfg SPEAKEASY_FOCUS_TAGS off
set_cfg SPEAKEASY_TRACK on
set_cfg SPEAKEASY_DOC_READ local
set_cfg SPEAKEASY_GROUND auto
set_cfg SPEAKEASY_TRACK_FPS 5
# old default (3) -> 5 scans/s while speaking
if grep -qx 'SPEAKEASY_TRACK_FPS=3' "$CONFIG" 2>/dev/null; then sed -i '' 's/^SPEAKEASY_TRACK_FPS=3$/SPEAKEASY_TRACK_FPS=5/' "$CONFIG"; fi
set_cfg SPEAKEASY_FOCUS_COLOR "#3B82F6"
set_cfg SPEAKEASY_FOCUS_OPACITY 0.22
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
local FOCUS_ON = cfg("SPEAKEASY_FOCUS", "on") ~= "off"
local LINES = "/tmp/speakeasy_cap_lines.json"
local TRACK_ON = FOCUS_ON and cfg("SPEAKEASY_TRACK", "on") ~= "off"
local TRACKER = os.getenv("HOME") .. "/.speakeasy/bin/speakeasy-track"
pcall(require, "hs.ipc")        -- lets the 'hs' command-line tool talk to Hammerspoon (e.g. hs -c 'hs.reload()')
-- speakeasy's own Hammerspoon log: ~/.speakeasy/logs/hs.log (also printed to the Hammerspoon console)
local function hslog(fmt, ...)
  local msg = string.format(fmt, ...)
  hs.printf("speakeasy: %s", msg)
  os.execute("mkdir -p '" .. os.getenv("HOME") .. "/.speakeasy/logs'")
  local f = io.open(os.getenv("HOME") .. "/.speakeasy/logs/hs.log", "a")
  if f then f:write(os.date("%Y-%m-%d %H:%M:%S ") .. msg .. "\n"); f:close() end
end
hslog("config loaded (focus=%s, track=%s, tracker binary %s)", tostring(FOCUS_ON), tostring(TRACK_ON),
      hs.fs.attributes(TRACKER) and "present" or "MISSING")

-- The focus box is drawn only by the native tracker (~/.speakeasy/bin/speakeasy-track: an NSPanel moved in-process by
-- scroll/AX events). Hammerspoon only owns the hotkey and starts/stops the tracker and the backend; it draws nothing.
-- Stop old Hammerspoon box machinery left over from before a reload.
for _, n in ipairs({ "speakeasyScrollTap", "speakeasyKeyTap", "speakeasyFocusWatcher", "speakeasyTapGate", "speakeasyNativeTimer" }) do
  if _G[n] then pcall(function() _G[n]:stop() end); _G[n] = nil end
end
for _, n in ipairs({ "speakeasyAppWatcher" }) do if _G[n] then pcall(function() _G[n]:stop() end); _G[n] = nil end end
if speakeasyWinFilter then pcall(function() speakeasyWinFilter:unsubscribeAll() end); speakeasyWinFilter = nil end
local srcWinId, srcPid = nil, nil
-- The tracker runs detached (nohup) so a Hammerspoon reload never kills an active read; its pid lives in tracker.pid.
local TRACKER_PID = os.getenv("HOME") .. "/.speakeasy/tracker.pid"
local pidCache = { t = 0 }
function speakeasyTrackerPid()
  local nowT = hs.timer.secondsSinceEpoch()
  if nowT - pidCache.t < 1 then return pidCache.p end      -- hs.execute blocks the Lua thread: check at most once a second
  pidCache.t = nowT; pidCache.p = nil
  local f = io.open(TRACKER_PID, "r"); if not f then return nil end
  local p = tonumber((f:read("*l") or ""):match("%d+")); f:close()
  -- os.execute's status is unreliable inside Hammerspoon (that made every start log FAILED); hs.execute reports it
  if p then local _, ok = hs.execute("/bin/kill -0 " .. p .. " 2>/dev/null"); if ok then pidCache.p = p; return p end end
  return nil
end
function speakeasyTrackerKill()
  local p = speakeasyTrackerPid()
  if p then hs.execute("/bin/kill " .. p .. " 2>/dev/null") end
  os.remove(TRACKER_PID); pidCache.t = 0
end
local function startTracker(win)
  speakeasyTrackerKill()
  srcWinId = win and win:id() or nil
  srcPid = win and win:application() and win:application():pid() or nil
  if not TRACK_ON then hslog("tracker off (SPEAKEASY_TRACK)"); return end
  if not win then hslog("tracker not started: no focused window"); return end
  if not hs.fs.attributes(TRACKER) then hslog("tracker not started: %s missing (re-run install.sh)", TRACKER); return end
  local app = win:application()
  local args = { "--window-id", tostring(win:id()), "--pid", tostring(app and app:pid() or 0),
    "--home", os.getenv("HOME") .. "/.speakeasy", "--fps", cfg("SPEAKEASY_TRACK_FPS", "5"),
    "--overlay", "native", "--run-id", tostring(speakeasyRunId) }
  local q = {}
  for _, a in ipairs(args) do q[#q + 1] = "'" .. tostring(a):gsub("'", "'\\''") .. "'" end
  local cmd = string.format("/usr/bin/nohup '%s' %s >/dev/null 2>&1 & echo $! > '%s'", TRACKER, table.concat(q, " "), TRACKER_PID)
  hs.execute(cmd)
  local ok = speakeasyTrackerPid() ~= nil
  hslog("tracker start %s: window %s pid %s run %s", ok and "ok" or "FAILED", tostring(win:id()), tostring(app and app:pid()), tostring(speakeasyRunId))
end

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
-- Per visual line bounds of the selection (for the focus highlight), written to LINES as JSON.
local function writeLineBounds(el, range)
  pcall(function()
    local first = el:parameterizedAttributeValue("AXLineForIndex", range.location)
    local last = el:parameterizedAttributeValue("AXLineForIndex", range.location + range.length - 1)
    if not first or not last or last < first or last - first > 300 then return end
    local out = {}
    for ln = first, last do
      local lr = el:parameterizedAttributeValue("AXRangeForLine", ln)
      if lr then
        local s0 = math.max(lr.location, range.location)
        local s1 = math.min(lr.location + lr.length, range.location + range.length)
        if s1 > s0 then
          local b = el:parameterizedAttributeValue("AXBoundsForRange", { location = s0, length = s1 - s0 })
          if goodRect(b) then table.insert(out, { loc = s0 - range.location, abs = s0, len = s1 - s0, rect = { b.x, b.y, b.w, b.h } }) end
        end
      end
    end
    if #out > 0 then
      local f = io.open(LINES, "w")
      if f then f:write(hs.json.encode(out)); f:close() end
    end
  end)
end

local function boundsOf(el)
  local ok, r = pcall(function()
    local range = el:attributeValue("AXSelectedTextRange")
    if range and (range.length or 0) > 0 then
      local b = el:parameterizedAttributeValue("AXBoundsForRange", range)
      if goodRect(b) then
        if FOCUS_ON then writeLineBounds(el, range) end
        return b
      end
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

-- Document-aware focus: tell speakeasy which document the window shows (read-only AX queries, no AppleScript).
local DOCHINT = "/tmp/speakeasy_cap_doc.json"
local DOC_READ = cfg("SPEAKEASY_DOC_READ", "local")
local function writeDocHint(win)
  os.remove(DOCHINT)
  if DOC_READ == "off" or not win or not hs.accessibilityState() then return end
  pcall(function()
    local app = win:application()
    local h = { app = app and app:name() or "", bundle = app and app:bundleID() or "", title = win:title() or "" }
    local axw = hs.axuielement.windowElement(win)
    if axw then h.ax_document = axw:attributeValue("AXDocument") end
    -- browsers: the URL of the web area that holds the focused element
    local el = hs.axuielement.systemWideElement():attributeValue("AXFocusedUIElement")
    for _ = 1, 40 do
      if not el then break end
      if el:attributeValue("AXRole") == "AXWebArea" then
        local u = el:attributeValue("AXURL"); h.url = type(u) == "table" and u.url or (type(u) == "string" and u or nil); break
      end
      el = el:attributeValue("AXParent")
    end
    local f = io.open(DOCHINT, "w"); if f then f:write(hs.json.encode(h)); f:close() end
  end)
end

local function run(args, method, rect, t0)
  if hs.fs.attributes(DOCHINT) then table.insert(args, "--doc-hint"); table.insert(args, DOCHINT) end
  table.insert(args, "--capture-method"); table.insert(args, method)
  if rect then table.insert(args, "--capture-rect"); table.insert(args, string.format("%d,%d,%d,%d", rect.x, rect.y, rect.w, rect.h)) end
  if method == "bounds" and hs.fs.attributes(LINES) then table.insert(args, "--line-bounds"); table.insert(args, LINES) end
  table.insert(args, "--capture-ms"); table.insert(args, tostring(math.floor((hs.timer.secondsSinceEpoch() - t0) * 1000)))
  table.insert(args, "--run-id"); table.insert(args, tostring(speakeasyRunId)); table.insert(args, "--app"); table.insert(args, "cli")
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

if speakeasyHotkey then speakeasyHotkey:delete() end
speakeasyHotkey = hs.hotkey.bind({ "ctrl", "alt" }, "S", function()
  speakeasyRunId = string.format("%x%04x", math.floor(hs.timer.secondsSinceEpoch() * 1000), math.random(0, 65535))
  os.remove("/tmp/speakeasy_sel.png"); os.remove("/tmp/speakeasy_win.png")   -- legacy capture files
  local pidf = os.getenv("HOME") .. "/.speakeasy/speak.pid"
  if hs.fs.attributes(pidf) then
    speakeasyTrackerKill()
    hs.task.new(SPEAKEASY, nil, { "stop" }):start(); return
  end
  os.remove(LINES)
  local t0 = hs.timer.secondsSinceEpoch()
  startTracker(hs.window.focusedWindow())      -- lock onto the source window now; it idles until the first sentence plays
  writeDocHint(hs.window.focusedWindow())
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
-- The Speakeasy menu-bar app owns the hotkey once SPEAKEASY_HOTKEY=app is set in ~/.speakeasy/config.
if cfg("SPEAKEASY_HOTKEY", "hammerspoon") == "app" and speakeasyHotkey then speakeasyHotkey:delete(); speakeasyHotkey = nil; hslog("hotkey handed to Speakeasy.app") end
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
