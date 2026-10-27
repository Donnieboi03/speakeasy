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
uv pip install --python "$PY" --break-system-packages -q pypdfium2 pillow onnxruntime numpy
# PDF layout model: verified into the shared download cache, then shipped in Resources/models (never built without it)
MODELS_CACHE="${SPEAKEASY_HOME:-$HOME/.speakeasy}/models"
"$PY" ../src/layoutdet.py fetch "$MODELS_CACHE" || { echo "ERROR: bundle needs a verified layout model; not building $APP"; exit 1; }
rm -rf "$RES/models"; mkdir -p "$RES/models"
cp "$MODELS_CACHE/pp_doclayout_s.onnx" "$RES/models/"
"$PY" -c 'import sys; sys.path.insert(0, "../src"); import layoutdet; sys.exit(not layoutdet.model_verified(sys.argv[1]))' "$RES/models/pp_doclayout_s.onnx" \
  || { echo "ERROR: layout model copy in $RES/models failed verification"; exit 1; }
# Block classifier model (Docling DocumentFigureClassifier-v2.5, MIT): same rules
"$PY" ../src/blockcls.py fetch "$MODELS_CACHE" || { echo "ERROR: bundle needs a verified block classifier model; not building $APP"; exit 1; }
cp "$MODELS_CACHE/docling_figure_classifier_v2_5.onnx" "$RES/models/"
"$PY" -c 'import sys; sys.path.insert(0, "../src"); import blockcls; sys.exit(not blockcls.model_verified(sys.argv[1]))' "$RES/models/docling_figure_classifier_v2_5.onnx" \
  || { echo "ERROR: block classifier model copy in $RES/models failed verification"; exit 1; }
# Second layout detector for gap pages (Docling layout heron, Apache-2.0, ~171 MB): same rules
"$PY" ../src/blockcls.py fetch-layout2 "$MODELS_CACHE" || { echo "ERROR: bundle needs a verified second layout model; not building $APP"; exit 1; }
cp "$MODELS_CACHE/docling_layout_heron.onnx" "$RES/models/"
"$PY" -c 'import sys; sys.path.insert(0, "../src"); import blockcls; sys.exit(not blockcls.layout2_verified(sys.argv[1]))' "$RES/models/docling_layout_heron.onnx" \
  || { echo "ERROR: second layout model copy in $RES/models failed verification"; exit 1; }
[ "$WITH_TTS" = "--with-tts" ] && uv pip install --python "$PY" --break-system-packages -q "mlx-audio[server]" "misaki[en]" webrtcvad-wheels uvicorn fastapi python-multipart num2words
rm -rf "$RES/backend"; mkdir -p "$RES/backend"
cp ../src/*.py "$RES/backend/"
cat > "$RES/backend/speakeasy" <<'SH'
#!/bin/sh
# launcher for the bundled backend: bundled CPython, bundled tracker
B="$(cd "$(dirname "$0")" && pwd)"; R="$(dirname "$B")"
export SPEAKEASY_DOC_PYTHON="${SPEAKEASY_DOC_PYTHON:-$R/python/bin/python3}"
export SPEAKEASY_TRACKER="${SPEAKEASY_TRACKER:-$(dirname "$R")/Helpers/SpeakeasyTracker.app/Contents/MacOS/speakeasy-track}"
exec "$R/python/bin/python3" "$B/speakeasy.py" "$@"
SH
chmod +x "$RES/backend/speakeasy"
find "$RES" -name __pycache__ -type d -prune -exec rm -rf {} +
codesign --force -s - -i com.donnieboi03.speakeasy --requirements '=designated => identifier "com.donnieboi03.speakeasy"' "$APP"
echo "bundled Python $("$PY" -c 'import sys;print(sys.version.split()[0])') into $APP"
