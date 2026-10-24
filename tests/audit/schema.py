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
    "capture_ms": (NUM, _sel, "screenshot stage"),
    "capture_method": ((str,), _sel, "bounds|highlight|select|mouse|none"),
    "capture_rect": ((list, str, dict, type(None)), _sel, "x,y,w,h captured"),
    "image": ((bool,), None, "image supplied & valid"),
    "image_px": ((str, type(None)), None, "original WxH"), "image_sent_px": ((str, type(None)), _img, "sent WxH"),
    "vision_model": ((str, type(None)), _img, "vision model"), "vision_ms": (NUM + (type(None),), _img, "vision stage"),
    "tts_first_audio_ms": (NUM + (type(None),), _spoken, "time to first audio"),
    "speak_ms": (NUM + (type(None),), _spoken, "speech duration"),
    "dry_run": ((bool,), _spk, "dry-run flag (decides whether tts/speak timings are required)"),
    "capture_path": ((str, type(None)), _img, "kept capture file path"),
    "tts": ((str, type(None)), _spoken, "engine actually used: say|kokoro"),
    "tts_requested": ((str, type(None)), _spoken, "engine requested via env/config"),
    "tts_voice": ((str, type(None)), lambda l: l.get("tts") == "kokoro", "voice used"),
    "tts_synth_ms": (NUM + (type(None),), lambda l: l.get("tts") == "kokoro", "total synth time"),
    "tts_server_rss_mb": (NUM + (type(None),), lambda l: l.get("tts_requested") == "kokoro", "TTS server memory"),
    "llm_error": ((str, type(None)), lambda l: l.get("command") not in ("stop", "last", "stats"), "LLM-stage error"),
    "copy_error": ((str, type(None)), _sel, "clipboard/copy-stage error"),
    "capture_error": ((str, type(None)), _sel, "screenshot/capture-stage error"),
    "tts_error": ((str, type(None)), _spk, "TTS-stage error"),
    "capture_original_px": ((str, type(None)), lambda l: bool(l.get("capture_rect")) and bool(l.get("image")), "pre-crop WxH"),
    "capture_cropped_px": ((str, type(None)), lambda l: bool(l.get("capture_rect")) and bool(l.get("image")), "post-crop WxH"),
    "capture_crop": ((str, type(None)), lambda l: bool(l.get("capture_rect")) and bool(l.get("image")), "cropped|already-cropped"),
    "vision_gate": ((str, type(None)), lambda l: l.get("command") in ("rewrite", "speak", "selection"), "auto-skip-prose|auto-use|forced|never|null(no image)"),
    "vision_gate_reason": ((str, type(None)), lambda l: l.get("vision_gate") is not None, "why the gate decided"),
    "streamed": ((bool,), lambda l: _spoken(l), "LLM output streamed into TTS"),
    "stream_note": ((str, type(None)), lambda l: _spoken(l), "why streaming was/wasn't used"),
    "llm_first_token_ms": (NUM + (type(None),), lambda l: l.get("streamed") is True, "time to first LLM token"),
    "first_sentence_ms": (NUM + (type(None),), lambda l: l.get("streamed") is True, "time to first full sentence"),
    "meta_refs_removed": ((int,), lambda l: l.get("backend") == "llm", "count of screenshot/image references stripped"),
    "vision_reasked": ((bool, type(None)), lambda l: str(l.get("capture_method", "")).startswith("no-selection") and l.get("image"), "no-selection answer re-asked"),
    "notes": ((list, str, type(None)), lambda l: l.get("command") in ("rewrite", "speak", "selection"), "non-error corrections (e.g. superlative fixes)"),
    "peak_rss_mb": (NUM, None, "peak RSS of speakeasy process"),
    "ollama_models": ((list, type(None)), lambda l: l.get("backend") == "llm" or bool(l.get("vision_ms")), "from /api/ps: loaded models + memory"),
}
CAPTURE_METHODS = {"bounds", "highlight", "select", "mouse", "none", "no-selection-window", "no-selection-screen"}

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
    if l.get("capture_method") is not None and l["capture_method"] not in CAPTURE_METHODS: probs.append(f"bad_capture_method:{l['capture_method']}")
    if str(l.get("capture_method", "")).startswith("no-selection") and not l.get("image"): probs.append("no_selection_method_without_image")
    if l.get("capture_method") == "none" and l.get("image"): probs.append("capture_none_but_image_used")
    if l.get("streamed") is True and isinstance(l.get("llm_first_token_ms"), NUM) and isinstance(l.get("first_sentence_ms"), NUM) and l["first_sentence_ms"] < l["llm_first_token_ms"]: probs.append("first_sentence_before_first_token")
    g = l.get("vision_gate")
    if g not in (None, "auto-skip-prose", "auto-use", "forced", "never"): probs.append(f"bad_vision_gate:{g}")
    if g in ("auto-skip-prose", "never") and (l.get("image") or l.get("vision_ms") or l.get("image_px") or l.get("capture_path")): probs.append("gate_skip_but_image_fields_set")
    if g in ("auto-use", "forced") and l.get("image") is False and not l.get("error"): probs.append("gate_use_but_no_image_and_no_error")
    if g and not l.get("vision_gate_reason"): probs.append("vision_gate_without_reason")
    le = str(l.get("llm_error") or "").lower()
    if any(w in le for w in ("superlative", "corrected", "contradict")): probs.append("correction_logged_as_llm_error")
    if le.startswith(("copy", "capture")) or "clipboard" in le or "pbpaste" in le: probs.append("non_llm_error_in_llm_error")
    if not l.get("image") and l.get("capture_path"): probs.append("capture_path_set_without_image")
    if l.get("capture_crop") not in (None, "cropped", "already-cropped"): probs.append(f"bad_capture_crop:{l.get('capture_crop')}")
    for st in ("copy", "capture", "llm", "tts"):
        tags = ("llm", "vision") if st == "llm" else (st,)
        if l.get(f"{st}_error") and not (l.get("error") and any(t in str(l.get("error")).lower() for t in tags)): probs.append(f"{st}_error_not_in_error")
    if _spoken(l) and l.get("tts") and l.get("tts_requested") and l["tts"] != l["tts_requested"] and not l.get("error"):
        probs.append("tts_fallback_without_error")
    return probs
