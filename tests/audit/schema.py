"""Log fields required for auditability. 'when' = predicate on the log line deciding if the field is required."""
NUM = (int, float)
def _img(l): return bool(l.get("image"))
def _spk(l): return l.get("command") in ("speak", "selection")
def _spoken(l): return _spk(l) and l.get("dry_run") is False
def _sel(l): return l.get("command") == "selection"
REQUIRED = {
    # field: (types, when, description)
    "ts": ((str, int, float), None, "run timestamp"),
    "command": ((str,), None, "rewrite|speak|selection|stop|last|stats"),
    "input_chars": ((int,), None, "chars in"), "output_chars": ((int,), None, "chars out"),
    "backend": ((str,), None, "llm|fallback"), "model": ((str, type(None)), None, "text model"),
    "error": ((str, type(None)), None, "error or null"),
    "total_ms": (NUM, None, "wall time"),
    "llm_ms": (NUM + (type(None),), None, "LLM stage"),
    "copy_ms": (NUM + (type(None),), _sel, "clipboard copy stage"),
    "capture_ms": (NUM + (type(None),), _sel, "screenshot stage"),
    "capture_method": ((str,), _sel, "bounds|highlight|select|mouse|none"),
    "capture_rect": ((list, str, dict, type(None)), _sel, "x,y,w,h captured"),
    "image": ((bool,), None, "image supplied & valid"),
    "image_px": ((str, type(None)), None, "original WxH"), "image_sent_px": ((str, type(None)), _img, "sent WxH"),
    "vision_model": ((str, type(None)), _img, "vision model"), "vision_ms": (NUM + (type(None),), _img, "vision stage"),
    "tts_first_audio_ms": (NUM + (type(None),), _spoken, "time to first audio"),
    "speak_ms": (NUM + (type(None),), _spoken, "speech duration"),
    "dry_run": ((bool,), _spk, "dry-run flag (decides whether tts/speak timings are required)"),
    "capture_path": ((str, type(None)), _img, "kept capture file path"),
    "peak_rss_mb": (NUM, None, "peak RSS of speakeasy process"),
    "ollama_models": ((list, type(None)), lambda l: l.get("backend") == "llm" or bool(l.get("vision_ms")), "from /api/ps: loaded models + memory"),
}
CAPTURE_METHODS = {"bounds", "highlight", "select", "mouse", "none"}

def flatten(l):
    t = l.get("timings")
    if isinstance(t, dict):
        l = dict(l)
        for k, v in t.items(): l.setdefault(k, v)
    return l

def validate(l):
    l = flatten(l); probs = []
    for k, (types, when, _) in REQUIRED.items():
        if when and not when(l): continue
        if k not in l: probs.append(f"missing:{k}")
        elif not isinstance(l[k], types) or (isinstance(l[k], bool) and bool not in types): probs.append(f"type:{k}={type(l[k]).__name__}")
    if "capture_method" in l and l["capture_method"] not in CAPTURE_METHODS: probs.append(f"bad_capture_method:{l['capture_method']}")
    return probs
