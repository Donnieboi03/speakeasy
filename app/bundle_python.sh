#!/bin/bash
# Put a self-contained Python backend inside Speakeasy.app (single install, no Homebrew Python needed):
#   python-build-standalone via uv (relocatable CPython) -> Contents/Resources/python
#   backend sources                                       -> Contents/Resources/backend (+ a `speakeasy` launcher)
# Usage: bash bundle_python.sh build/Speakeasy.app [--with-tts]   (--with-tts also installs Kokoro / mlx-audio, ~1.5 GB)
set -euo pipefail
cd "$(dirname "$0")"
APP="${1:?usage: bundle_python.sh path/to/Speakeasy.app [--with-tts]}"; WITH_TTS="${2:-}"
command -v uv >/dev/null || { echo "uv not found: brew install uv (or curl -LsSf https://astral.sh/uv/install.sh | sh)"; exit 1; }
RES="$APP/Contents/Resources"; PYV="${SPEAKEASY_PYTHON_VERSION:-3.12}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
uv python install "$PYV" --install-dir "$TMP/py" >/dev/null
SRCPY="$(dirname "$(dirname "$(ls "$TMP"/py/*/bin/python3 | head -1)")")"
rm -rf "$RES/python"; cp -R "$SRCPY" "$RES/python"
PY="$RES/python/bin/python3"
uv pip install --python "$PY" --break-system-packages -q pypdfium2 pillow
[ "$WITH_TTS" = "--with-tts" ] && uv pip install --python "$PY" --break-system-packages -q "mlx-audio[server]" "misaki[en]" webrtcvad-wheels uvicorn fastapi python-multipart num2words
rm -rf "$RES/backend"; mkdir -p "$RES/backend"
cp ../src/speakeasy.py ../src/highlight.py ../src/docmap.py ../src/ground.py ../src/stats.py ../src/pdfpage.py "$RES/backend/"
cat > "$RES/backend/speakeasy" <<'SH'
#!/bin/sh
# launcher for the bundled backend: bundled CPython, bundled tracker
B="$(cd "$(dirname "$0")" && pwd)"; R="$(dirname "$B")"
export SPEAKEASY_DOC_PYTHON="${SPEAKEASY_DOC_PYTHON:-$R/python/bin/python3}"
export SPEAKEASY_TRACKER="${SPEAKEASY_TRACKER:-$(dirname "$R")/MacOS/speakeasy-track}"
exec "$R/python/bin/python3" "$B/speakeasy.py" "$@"
SH
chmod +x "$RES/backend/speakeasy"
find "$RES" -name __pycache__ -type d -prune -exec rm -rf {} +
codesign --force --deep -s - "$APP"
echo "bundled Python $("$PY" -c 'import sys;print(sys.version.split()[0])') into $APP"
