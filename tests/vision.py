"""Image-input grid for speakeasy (black-box)."""
import json, os, re, tempfile, time
from PIL import Image
from checks import MD_PATTERNS, URL_RE, nums, LOG_KEYS, check_rank
import make_images
IMG_KEYS = ["image", "image_px", "image_sent_px", "vision_model", "vision_ms"]

def _valid_px(path):
    try:
        with Image.open(path) as im: im.verify()
        with Image.open(path) as im: return f"{im.size[0]}x{im.size[1]}"
    except Exception: return None

def _norm(s): return re.sub(r"[^a-z0-9]+", " ", s.lower())

def checks(name, text, path, meta, rc, out, err, log, cond, dt=0):
    r = []
    def add(c, ok, d=""): r.append({"check": c, "pass": bool(ok), "detail": str(d)[:160]})
    px = _valid_px(path) if os.path.exists(path) else None
    add("exit_0", rc == 0, f"rc={rc}")
    if text.strip() or (px and cond == "vision"): add("nonempty_output", out.strip() != "")
    for k, p in MD_PATTERNS.items():
        m = re.search(p, out); add(f"no_md_{k}", not m, m.group(0) if m else "")
    m = URL_RE.search(out); add("no_raw_url", not m, m.group(0) if m else "")
    known = nums(text) | set(meta.get("nums", [])); bad = []
    for n in {x.replace(",", "") for x in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", out)}:
        if n in known or n in text.replace(",", ""): continue
        if meta.get("x1000") and float(n) % 1000 == 0 and (str(int(float(n)) // 1000) in known): continue
        if float(n).is_integer() and float(n) <= 12: continue
        bad.append(n)
    add("no_invented_numbers", not bad, ",".join(sorted(bad)))
    r += check_rank(name, out)
    add("run_under_90s", dt <= 90, f"{dt:.1f}s")
    if meta.get("bad"): add("bad_image_warns_stderr", err.strip() != "", err[:100])
    if cond == "unreachable": add("fallback_warns_stderr", err.strip() != "", err[:100])
    if "order" in meta and cond == "vision":
        o = _norm(out); st = [_norm(x).strip() for x in meta["order"]]
        found = [x for x in st if x in o]
        def chain(start_stage_idx, pos):
            n, p = 1, pos
            for x in st[start_stage_idx + 1:]:
                q = o.find(x, p + 1)
                if q >= 0: n += 1; p = q
            return n
        best = 0
        for i, x in enumerate(st):
            p = o.find(x)
            while p >= 0: best = max(best, chain(i, p)); p = o.find(x, p + 1)
        add("pipeline_mentions_4plus_stages", len(found) >= 4, found)
        add("pipeline_stage_order", best >= len(found), f"longest ordered chain {best} of {len(found)} mentioned")
    if meta.get("trend") == "up" and cond == "vision":
        up = re.search(r"\b(grow|grew|growth|increas|ris|rose|climb|up|upward|steadily higher)", out, re.I)
        down = re.search(r"\b(declin|decreas|fell|fall|drop|down)", out, re.I)
        add("trend_direction_up", up and not down, out[:120])
    if cond == "vision" and px:
        mm = re.search(r"\b(screenshot|image|picture|photo|capture|screen|i can see)\b", out, re.I)
        exempt = mm and re.search(r"\b" + re.escape(mm.group(0)) + r"\b", text, re.I)
        add("no_meta_refs_to_image", not mm or exempt, mm.group(0) if mm else "")
    if meta.get("inject"):
        add("injection_not_obeyed", not out.strip().lower().strip(".'\"!").startswith("banana"), out[:100])
    if meta.get("chrome") and cond == "vision":
        hit = [c for c in meta["chrome"] if c in out.lower()]
        add("ignores_ui_chrome", not hit, hit)
    # logs
    if log is None: add("log_line_written", False); return r
    if log.get("vision_gate") in ("auto-skip-prose", "never"):
        add("gate_skip_image_fields_empty", not log.get("image") and not log.get("image_px") and not log.get("vision_ms"), log.get("vision_gate"))
        px = None
    miss = [k for k in LOG_KEYS + IMG_KEYS if k not in log]; add("log_schema_keys_incl_image", not miss, miss)
    add("log_image_bool", log.get("image") is (px is not None), f"image={log.get('image')} valid={px is not None}")
    add("log_image_px", log.get("image_px") == px, f"{log.get('image_px')} vs {px}")
    if px:
        sp = log.get("image_sent_px")
        ok = isinstance(sp, str) and re.fullmatch(r"\d+x\d+", sp) and all(int(a) <= int(b) for a, b in zip(sp.split("x"), px.split("x")))
        add("log_image_sent_px_valid", ok, f"{sp} (orig {px})")
    add("log_total_ms_number", isinstance(log.get("total_ms"), (int, float)))
    if cond == "vision" and px and log.get("vision_gate") not in ("auto-skip-prose", "never"):
        add("log_vision_ms_recorded", isinstance(log.get("vision_ms"), (int, float)) and log["vision_ms"] > 0, log.get("vision_ms"))
        add("log_vision_model", bool(log.get("vision_model")), log.get("vision_model"))
    exp = {"unreachable": "fallback"}.get(cond, "llm")
    if cond == "vision_unavail" and not text.strip(): exp = "fallback"
    if cond == "vision_unavail" and not text.strip(): add("image_only_no_vision_empty_output", out.strip() == "", out[:80])
    add("log_backend", log.get("backend") == exp, log.get("backend"))
    blob = json.dumps(log, ensure_ascii=False)
    add("log_no_text_by_default", not (len(text) >= 20 and text[:20] in blob))
    return r

def run_grid(run, read_logs, good_url, bad_url, llm_up, vision_model, vision_up, cmds=("rewrite",)):
    res = []; X = make_images.build()
    conds = {"vision": (good_url, vision_model), "vision_unavail": (good_url, "no-such-vision-model:0b"), "unreachable": (bad_url, vision_model)}
    for cond, (url, vm) in conds.items():
        for name, (text, path, meta) in X.items():
            if os.environ.get("VISION_FILTER") and not any(f in name for f in os.environ["VISION_FILTER"].split(",")): continue
            rec = {"fixture": name, "condition": cond, "checks": []}
            if cond != "unreachable" and not llm_up: rec["skipped"] = "LLM down"; res.append(rec); continue
            if cond == "vision" and not vision_up: rec["skipped"] = f"vision model {vm} unavailable"; res.append(rec); continue
            home = tempfile.mkdtemp(prefix="se_img_")
            env = dict(os.environ, SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LLM_URL=url, SPEAKEASY_VISION_MODEL=vm)
            env.pop("SPEAKEASY_LOG_TEXT", None)
            rc, out, err, dt = run(["rewrite", "--image", path], text, env, timeout=420)
            logs = read_logs(home)
            rec.update(rc=rc, secs=round(dt, 1), output=out[:2000], stderr=err[:400], text_in=text[:200], image=os.path.basename(path), log=logs[-1] if logs else None)
            rec["checks"] = checks(name, text, path, meta, rc, out, err, logs[-1] if logs else None, cond, dt)
            res.append(rec)
    # speak --image --dry-run contract
    home = tempfile.mkdtemp(); env = dict(os.environ, SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LLM_URL=bad_url)
    rc, out, err, _ = run(["speak", "--image", X["img_bar"][1], "--dry-run"], X["img_bar"][0], env, timeout=120)
    res.append({"fixture": "speak_image_dry_run", "condition": "unreachable", "output": out[:300], "stderr": err[:200], "checks": [
        {"check": "exit_0", "pass": rc == 0, "detail": f"rc={rc}"}, {"check": "prints", "pass": bool(out.strip()), "detail": ""}]})
    return res
