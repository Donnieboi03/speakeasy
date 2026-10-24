#!/usr/bin/env python3
"""Black-box focus-highlight tests."""
import json, os, re, shutil, statistics, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); sys.path.insert(0, AUD); sys.path.insert(0, HERE)
from schema import validate, flatten
from audit_focus import audit
sys.path.insert(0, os.path.dirname(AUD))
from checks import BROKEN
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../src/speakeasy")); REAL = os.environ.get("TEST_LLM_URL", "http://localhost:11434/v1")
PORT = 18892; MOCK = f"http://127.0.0.1:{PORT}/v1"; IMGS = os.path.join(os.path.dirname(AUD), "images"); R = []; SCORES = {}
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:260]})
def jl(p): return [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []
SRV = []
def mock(**kw):
    for s in SRV: s.terminate(); s.wait()
    SRV.clear(); SRV.append(subprocess.Popen([sys.executable, os.path.join(AUD, "stream", "mock_llm.py")], env={**os.environ, "MOCK_PORT": str(PORT), "MOCK_LOG": "/tmp/mock_focus.jsonl", "MOCK_TOKEN_DELAY": "0.01", "MOCK_FIRST_DELAY": "0.2", **{k: str(v) for k, v in kw.items()}}))
    time.sleep(0.7)
def env(llm=MOCK, **kw):
    h = tempfile.mkdtemp(prefix="se_focus_")
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=llm, SPEAKEASY_MODEL="mock-text" if llm == MOCK else "qwen2.5:3b-instruct",
             SPEAKEASY_VISION_MODEL="mock-vision" if llm == MOCK else "qwen2.5vl:3b", SPEAKEASY_FOCUS="on",
             PATH=os.path.join(AUD, "nosel", "stubs") + ":" + os.path.join(AUD, "tts", "stubs") + ":" + os.environ["PATH"],
             STUB_LOG=os.path.join(h, "stub.jsonl"), STUB_SPOKEN=os.path.join(h, "spoken.txt"), STUB_PLAY_SEC="0.2")
    for k in ("SPEAKEASY_FOCUS_TAGS", "SPEAKEASY_STREAM", "SPEAKEASY_VISION"): e.pop(k, None)
    e.update({k: str(v) for k, v in kw.items()}); return h, e
N = [0]
def cap(src):
    N[0] += 1; p = f"/tmp/speakeasy_cap_focus{os.getpid()}_{N[0]}.png"; shutil.copy(src, p); return p
def sel(h, e, text, rect="100,200,700,300", img=os.path.join(IMGS, "table.png"), method="bounds", timeout=300):
    e = dict(e, STUB_CLIP=text); t = time.time()
    p = subprocess.run([BIN, "selection", "--image", cap(img), "--capture-method", method, "--capture-rect", rect], capture_output=True, text=True, env=e, timeout=timeout)
    runs = jl(os.path.join(h, "logs", "runs.jsonl")); l = flatten(runs[-1]) if runs else {}
    return p, l, jl(os.path.join(h, "logs", "focus.jsonl")), jl(e["STUB_LOG"]), (open(e["STUB_SPOKEN"]).read() if os.path.exists(e["STUB_SPOKEN"]) else ""), time.time() - t
BROKEN = re.compile(r",\s*,|\bis also and\b|\b(and|or|but|while)\s*[.,]|\bthere is (two|three|four|five|\d+|many|several)\b|\b(the|a|an|is|at)\s*[.,]|\s[,.](?!\d)", re.I)
BRACKET = re.compile(r"[\[\]【】⟦⟧]|<\s*/?\s*L?\d+\s*>|\{\s*L?\d+\s*\}|\bL\d+\b", re.I)
# labelled fixtures: (text, mock answer, [expected source line per sentence])
FIX = {
    "table": ("| Plan | Price | Seats |\n|---|---|---|\n| Basic | $9 | 1 |\n| Team | $29 | 5 |\n| Enterprise | $99 | 50 |",
              "There are three plans. Basic costs 9 dollars for 1 seat. Team costs 29 dollars for 5 seats. Enterprise costs 99 dollars for 50 seats.", [None, 3, 4, 5]),
    "list": ("- Gather the raw recordings\n- Clean and re-reference the data\n- Train the classifier\n- Evaluate on held-out subjects",
             "First, gather the raw recordings. Next, clean and re-reference the data. Then train the classifier. Finally, evaluate on held-out subjects.", [1, 2, 3, 4]),
    "code": ("```python\ndef add(a, b):\n    return a + b\nprint(add(2, 3))\n```",
             "The add function takes a and b and returns their sum. The last line prints the result of adding 2 and 3.", [(2, 3), 4]),
    "prose": ("The deployment finished on Tuesday after a long delay.\nUsers reported faster page loads within an hour.\nThe team plans a retrospective next week.",
              "The deployment finished on Tuesday after a delay. Users then saw faster page loads. A retrospective is planned for next week.", [1, 2, 3]),
}
def score(maps, exp):
    hits = tot = 0; hits_tol = 0; det = []
    for i, x in enumerate(exp):
        if x is None: continue
        tot += 1; lo, hi = (x, x) if isinstance(x, int) else x
        m = maps[i] if i < len(maps) else None; ln = (m or {}).get("lines") or [None, None]
        ok = ln[0] is not None and ln[0] <= hi and ln[1] >= lo; okt = ln[0] is not None and ln[0] <= hi + 1 and ln[1] >= lo - 1
        hits += ok; hits_tol += okt; det.append((i, x, ln, (m or {}).get("method")))
    return hits / max(tot, 1), hits_tol / max(tot, 1), det
try:
    for tags in ("off", "on"):
        for name, (text, ans, exp) in FIX.items():
            for variant, a in (("plain", ans), ("tagged", " ".join(f"[L{e if isinstance(e, int) else (e[0] if e else 1)}] {s}" for s, e in zip(re.split(r"(?<=\.)\s+", ans), exp))),
                               ("garbage_tags", "[[L " + ans.replace(". ", ". [L99] ", 1) + " 【3】 <L2> {L4}")):
                if variant != "plain" and name != "table": continue
                mock(MOCK_TEXT=a, MOCK_VISION=a); h, e = env(SPEAKEASY_FOCUS_TAGS=tags, SPEAKEASY_VISION="never")
                p, l, fev, stub, spoken, dt = sel(h, e, text)
                tag = f"{tags}_{name}_{variant}"
                g = BROKEN.search(spoken); add(f"tags{tag}_no_broken_clauses", not g, g.group(0) if g else "")
                m = BRACKET.search(spoken); add(f"tags{tag}_no_brackets_spoken", not m and spoken.strip(), f"{m.group(0) if m else ''} :: {spoken[:120]}")
                maps = [x for x in fev if x.get("state") == "map"]; shows = [x for x in fev if x.get("state") == "show"]
                says = [x for x in stub if x.get("tool") == "say"]
                order_ok = len(shows) >= 1 and len(says) >= 1 and all(any(sw["ts"] <= sy["start"] + 0.005 and sw["text"][:30].lower() in sy["args"].lower() for sw in shows) for sy in says if sy["args"].strip())
                add(f"tags{tag}_event_before_audio_each_sentence", order_ok, f"shows={len(shows)} says={len(says)}")
                add(f"tags{tag}_focus_events_logged", l.get("focus_events") == len(shows), f"log={l.get('focus_events')} file={len(shows)}")
                add(f"tags{tag}_focus_tags_flag", l.get("focus_tags") is (tags == "on"), l.get("focus_tags"))
                if tags == "on": add(f"tags{tag}_tag_hit_rate_numeric", isinstance(l.get("focus_tag_hit_rate"), (int, float)), l.get("focus_tag_hit_rate"))
                fa = audit(fev, [100, 200, 700, 300]); add(f"tags{tag}_focus_file_audit_clean", not fa["problems"], fa["problems"][:3])
                add(f"tags{tag}_schema", not validate(l), validate(l))
                if variant == "plain":
                    srcl = text.split("\n")
                    def src_ok(sw, mp):
                        ln = (mp or {}).get("lines") or [None, None]
                        if not sw.get("src") or ln[0] is None: return False
                        want = " ".join(" ".join(re.sub(r"[|`*#-]", " ", srcl[i - 1]).split()) for i in range(ln[0], ln[1] + 1) if 0 < i <= len(srcl))
                        nz = lambda t: " ".join(re.sub(r"[|`*#-]", " ", t).split()).lower()
                        return nz(" ".join(sw["src"])) in nz(want) or nz(want) in nz(" ".join(sw["src"]))
                    add(f"tags{tag}_show_src_matches_lines", shows and all(src_ok(sw, mp) for sw, mp in zip(shows, maps)), [(sw.get("src"), mp.get("lines")) for sw, mp in zip(shows, maps)][:3])
                    add(f"tags{tag}_show_char_ranges_present", shows and all(any(k in sw for k in ("ranges", "char_ranges", "chars", "range")) for sw in shows), sorted(shows[0].keys()) if shows else None)
                if variant == "plain" or (variant == "tagged" and tags == "on"):
                    s, st, det = score(maps, exp); SCORES[tag] = {"exact": round(s, 2), "within1": round(st, 2), "detail": det}
                    add(f"tags{tag}_source_lines_plausible", st >= 0.6, f"exact={s:.2f} ±1={st:.2f} {det}")
                for f in os.listdir("/tmp"):
                    if f.startswith(f"speakeasy_cap_focus{os.getpid()}"): os.remove(os.path.join("/tmp", f))
    # focus off writes nothing
    mock(MOCK_TEXT=FIX["table"][1]); h, e = env(SPEAKEASY_FOCUS="off", SPEAKEASY_VISION="never"); p, l, fev, stub, spoken, dt = sel(h, e, FIX["table"][0])
    add("off_no_focus_json", not os.path.exists(os.path.join(h, "focus.json")), ""); add("off_no_focus_jsonl", not fev, len(fev))
    add("off_focus_events_zero", not l.get("focus_events"), l.get("focus_events")); add("off_still_speaks", spoken.strip(), spoken[:60])
    # stop clears focus
    mock(MOCK_TEXT=" ".join(f"Row {i} has value {i*7}." for i in range(1, 15)), MOCK_TOKEN_DELAY="0.1"); h, e = env(STUB_PLAY_SEC="2", STUB_CLIP=FIX["table"][0], SPEAKEASY_VISION="never")
    pr = subprocess.Popen([BIN, "selection", "--image", cap(os.path.join(IMGS, "table.png")), "--capture-method", "bounds", "--capture-rect", "100,200,700,300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=e)
    time.sleep(4); subprocess.run([BIN, "stop"], env=e, capture_output=True); time.sleep(1.5)
    try: pr.wait(5)
    except subprocess.TimeoutExpired: pr.kill()
    fj = json.load(open(os.path.join(h, "focus.json"))) if os.path.exists(os.path.join(h, "focus.json")) else None
    add("stop_focus_json_cleared", fj is None or fj.get("state") == "clear", fj)
    fev = jl(os.path.join(h, "logs", "focus.jsonl")); lc = max([i for i, x in enumerate(fev) if x.get("state") == "clear"], default=-1)
    add("stop_no_show_after_clear", lc >= 0 and not any(x.get("state") == "show" for x in fev[lc + 1:]), f"clear_idx={lc} n={len(fev)}")
    # TTFT focus on vs off
    mock(MOCK_TEXT=FIX["table"][1], MOCK_FIRST_DELAY="0.4", MOCK_TOKEN_DELAY="0.03"); tt = {"on": [], "off": []}
    for i in range(4):
        for f in ("on", "off"):
            h, e = env(SPEAKEASY_FOCUS=f, SPEAKEASY_VISION="never"); p, l, *_ = sel(h, e, FIX["table"][0])
            if isinstance(l.get("tts_first_audio_ms"), (int, float)): tt[f].append(l["tts_first_audio_ms"])
    mon, moff = (statistics.median(tt["on"]) if tt["on"] else None), (statistics.median(tt["off"]) if tt["off"] else None)
    add("ttft_focus_on_within_100ms_of_off", mon is not None and moff is not None and mon - moff <= 100, f"on={tt['on']} off={tt['off']} median diff={None if mon is None or moff is None else mon - moff}")
    TT = {"on": tt["on"], "off": tt["off"]}
    # vision-no-text: bounded rects inside capture
    mock(MOCK_VISION="A chat window shows a summary. The migration cut p95 latency from 480 ms to 210 ms. The sidebar lists settings and history.")
    for rect in ("50,80,1200,750", "300,100,600,375"):
        h, e = env(); p, l, fev, stub, spoken, dt = sel(h, e, "", rect=rect, img=os.path.join(IMGS, "screenshot.png"), method="no-selection-window")
        rr = [float(v) for v in rect.split(",")]; shows = [x for x in fev if x.get("state") == "show"]
        fa = audit(fev, rr); add(f"vision_notext_{rect}_rects_inside_capture", shows and not fa["problems"], f"shows={len(shows)} {fa['problems'][:3]}")
        add(f"vision_notext_{rect}_rects_not_full_capture_each", shows and all(s["rect"][2] * s["rect"][3] <= rr[2] * rr[3] * 1.0001 for s in shows), [s["rect"] for s in shows][:3])
        add(f"vision_notext_{rect}_fallback_logged", l.get("focus_fallback") is not None, l.get("focus_fallback"))

    # ---- adversarial brackets ----
    TBL = FIX["table"][0]
    ODD = {"range_tag": "[L3-5] Basic costs 9 dollars for 1 seat.", "lower_tag": "[l3] Basic costs 9 dollars for 1 seat.", "spaced_tag": "[ L3 ] Basic costs 9 dollars for 1 seat.",
           "double_tag": "[[L3]] Basic costs 9 dollars for 1 seat.", "cjk_tag": "【L3】 Basic costs 9 dollars for 1 seat.", "angle_tag": "<L3> Basic costs 9 dollars for 1 seat.",
           "curly_tag": "{L3} Basic costs 9 dollars for 1 seat.", "paren_tag": "(L3) Basic costs 9 dollars for 1 seat.", "word_tag": "[line 3] Basic costs 9 dollars for 1 seat.",
           "glued_front": "[L3]Basic costs 9 dollars for 1 seat.", "glued_back": "Basic costs 9 dollars for 1 seat[L3].", "orphan_open": "There are three plans [ Basic costs 9 dollars for 1 seat.",
           "orphan_close": "There are three plans ] Basic costs 9 dollars for 1 seat.", "mid_tag": "Basic costs [L3] 9 dollars for 1 seat.", "unclosed_tag": "[L3 Basic costs 9 dollars for 1 seat."}
    for nm, ans in ODD.items():
        for tags in ("off", "on"):
            mock(MOCK_TEXT=ans + " Team costs 29 dollars for 5 seats.", MOCK_VISION=ans); h, e = env(SPEAKEASY_FOCUS_TAGS=tags, SPEAKEASY_VISION="never")
            p, l, fev, stub, spoken, dt = sel(h, e, TBL)
            m = re.search(r"[\[\]【】<>{}]|\(L\d|\bL\d+\b|\bline 3\b", spoken, re.I)
            add(f"odd_{nm}_tags{tags}_no_tag_spoken", not m, f"{m.group(0) if m else ''} :: {spoken[:120]}")
            add(f"odd_{nm}_tags{tags}_content_kept", all(w in spoken for w in ("Basic", "9", "seat", "Team", "29")), spoken[:120])
            add(f"odd_{nm}_tags{tags}_focus_text_clean", not any(re.search(r"[\[\]【】<>{}]", x.get("text", "")) for x in fev if x.get("state") == "show"), "")
    LEGIT = {"code_index": ("x = arr[0] + sum([1,2,3])", "The code adds arr[0] to sum([1,2,3]).", ("arr", "0", "sum", "1", "2", "3")),
             "md_link": ("Read [the setup guide](https://example.com/setup) before installing.", "Read [the setup guide](https://example.com/setup) before installing.", ("setup guide", "before installing")),
             "array_literal": ("values = [10, 20, 30]", "The values list holds [10, 20, 30].", ("10", "20", "30")),
             "citation": ("Kokoro is fast [1].", "Kokoro is fast [1].", ("Kokoro", "fast")),
             "dict_access": ("cfg['rate']", "It reads cfg['rate'] from the config.", ("cfg", "rate"))}
    for nm, (src, ans, need) in LEGIT.items():
        for tags in ("off", "on"):
            mock(MOCK_TEXT=ans); h, e = env(SPEAKEASY_FOCUS_TAGS=tags, SPEAKEASY_VISION="never"); p, l, fev, stub, spoken, dt = sel(h, e, src)
            add(f"legit_{nm}_tags{tags}_content_spoken", all(w.lower() in spoken.lower() for w in need), f"{spoken[:140]}")
            add(f"legit_{nm}_tags{tags}_no_url", "http" not in spoken and "example.com" not in spoken, spoken[:100])
            add(f"legit_{nm}_tags{tags}_nothing_swallowed", len(spoken.strip()) >= 0.5 * len(re.sub(r"\(https?://[^)]*\)|[\[\]()'\"]", "", ans)), f"{len(spoken)} vs {len(ans)}")
    # ---- is/are grammar after demeta ----
    GR = ["I can see one bar.", "I can see 3 bars.", "I can see a chart with two lines.", "In this image there are four rows.", "The screenshot shows one table.",
          "I can see that there is a single spike.", "The picture shows many errors.", "I can see 12 items and one header."]
    for i, ans in enumerate(GR):
        mock(MOCK_TEXT=ans, MOCK_VISION=ans); h, e = env(SPEAKEASY_VISION="never"); p, l, fev, stub, spoken, dt = sel(h, e, TBL)
        bad = BROKEN.search(spoken) or re.search(r"\bthere (?:is|'s) (?:\d+|two|three|four|many|several|12)\b|\bthere are (?:one|a|an|1|a single)\b|\b(?:one|1|a single) \w+s are\b", spoken, re.I)
        meta = re.search(r"\b(screenshot|image|picture|i can see)\b", spoken, re.I)
        add(f"grammar_{i}", not bad and not meta and spoken.strip(), f"{bad.group(0) if bad else ''}{meta.group(0) if meta else ''} :: {spoken[:100]}")
    # ---- TTFT tables & steps, 5 runs ----
    TT5 = {}
    for kind in ("table", "list"):
        mock(MOCK_TEXT=FIX[kind][1], MOCK_FIRST_DELAY="0.4", MOCK_TOKEN_DELAY="0.03"); vals = {"on": [], "off": []}
        for i in range(5):
            for f in ("on", "off"):
                h, e = env(SPEAKEASY_FOCUS=f, SPEAKEASY_VISION="never"); p, l, *_ = sel(h, e, FIX[kind][0])
                if isinstance(l.get("tts_first_audio_ms"), (int, float)): vals[f].append(l["tts_first_audio_ms"])
        vt = []
        for i in range(5):
            h, e = env(SPEAKEASY_FOCUS="on", SPEAKEASY_TRACK="on", SPEAKEASY_VISION="never"); p, l, *_ = sel(h, e, FIX[kind][0])
            if isinstance(l.get("tts_first_audio_ms"), (int, float)): vt.append(l["tts_first_audio_ms"])
        add(f"ttft5_{kind}_track_on_within_100ms", vt and statistics.median(vt) - statistics.median(vals["off"]) <= 100, {"track_on": vt})
        add(f"track_on_cli_spawns_no_tracker_{kind}", not subprocess.run(["pgrep", "-f", "speakeasy-track"], capture_output=True).stdout, "")
        mon, moff = statistics.median(vals["on"]), statistics.median(vals["off"]); TT5[kind] = {"on": vals["on"], "off": vals["off"], "median_on": mon, "median_off": moff, "diff": mon - moff}
        add(f"ttft5_{kind}_within_100ms", mon - moff <= 100, TT5[kind])
    # real model smoke: tags on, table
    if SRV: SRV[0].terminate(); SRV.clear()
    for tags in ("off", "on"):
        h, e = env(llm=REAL, SPEAKEASY_FOCUS_TAGS=tags, SPEAKEASY_VISION="never"); p, l, fev, stub, spoken, dt = sel(h, e, FIX["table"][0])
        m = BRACKET.search(spoken); add(f"real_tags{tags}_no_brackets_spoken", not m and spoken.strip(), f"{m.group(0) if m else ''} :: {spoken[:150]}")
        maps = [x for x in fev if x.get("state") == "map"]; s, st, det = score(maps[1:] if len(maps) > 3 else maps, [3, 4, 5])
        SCORES[f"real_tags{tags}"] = {"exact": round(s, 2), "within1": round(st, 2), "methods": l.get("focus_methods"), "tag_hit_rate": l.get("focus_tag_hit_rate")}
        add(f"real_tags{tags}_audit_clean", not audit(fev, [100, 200, 700, 300])["problems"], audit(fev, [100, 200, 700, 300])["problems"][:2])
finally:
    for s in SRV: s.terminate()
    for f in os.listdir("/tmp"):
        if f.startswith(f"speakeasy_cap_focus{os.getpid()}"): os.remove(os.path.join("/tmp", f))
res = {"checks": R, "line_scores": SCORES, "ttft_ms": locals().get("TT"), "ttft5": locals().get("TT5"), "summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}}
json.dump(res, open(os.path.join(AUD, "reports", "focus.json"), "w"), indent=2, default=str)
md = ["# focus highlight tests", f"summary: {res['summary']}", f"ttft: {res['ttft_ms']}", f"ttft5: {res['ttft5']}", "", "## line scores", *[f"- {k}: {v.get('exact')} exact / {v.get('within1')} ±1 {v.get('methods','')} {v.get('tag_hit_rate','')}" for k, v in SCORES.items()], "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:150].replace('|','¦')} |" for c in R]
open(os.path.join(AUD, "reports", "focus.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
