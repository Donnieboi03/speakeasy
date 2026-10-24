#!/usr/bin/env python3
"""Black-box verbatim-mode tests: naming, word-level body fidelity, placeholders, false positives, mixed content, TTFT, focus mapping, off switch."""
import json, os, re, shutil, statistics, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); sys.path.insert(0, AUD); sys.path.insert(0, os.path.dirname(AUD))
from schema import validate, flatten
from checks import BROKEN
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(HERE, "../../../src/speakeasy")); URL = os.environ.get("TEST_LLM_URL", "http://localhost:11434/v1")
IMGS = os.path.join(os.path.dirname(AUD), "images"); R = []
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:260]})
def jl(p): return [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []
def env(**kw):
    h = tempfile.mkdtemp(prefix="se_verb_")
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=URL, SPEAKEASY_MODEL=os.environ.get("SPEAKEASY_MODEL", "qwen2.5:3b-instruct"), SPEAKEASY_VISION="never",
             PATH=os.path.join(AUD, "nosel", "stubs") + ":" + os.path.join(AUD, "tts", "stubs") + ":" + os.environ["PATH"],
             STUB_LOG=os.path.join(h, "stub.jsonl"), STUB_SPOKEN=os.path.join(h, "spoken.txt"), STUB_PLAY_SEC="0.1")
    e.pop("SPEAKEASY_VERBATIM", None); e.update({k: str(v) for k, v in kw.items()}); return h, e
def rw(text, **kw):
    h, e = env(**kw); p = subprocess.run([BIN, "rewrite"], input=text, capture_output=True, text=True, env=e, timeout=200)
    L = jl(os.path.join(h, "logs", "runs.jsonl")); return p.stdout, (flatten(L[-1]) if L else {}), p.returncode
def toks(s): return re.findall(r"[a-z0-9']+", s.lower())
def lcs_ratio(src, out):
    a, b = toks(src), toks(out)
    if not a: return 1.0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b): cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[-1]))
        prev = cur
    return prev[-1] / len(a)
PH_RAW = re.compile(r"\[[^\]]{2,40}\]|\bx{3,}\b|\b\d{3}-x{4}\b|\$\s?XX+|\bXX/XX/XXXX\b|_{3,}|<[a-z ]{3,30}>|\{[a-z ]{3,30}\}", re.I)
POS = {
 "email_headers": dict(text="Sure! Here's a draft you can send:\n\nTo: Dr. Patel\nSubject: EEG lab meeting reschedule\n\nHi Dr. Patel,\n\nI wanted to ask if we could move Thursday's lab meeting to Friday at 2 pm. I will bring the updated ear-EEG results and the filter comparison.\n\nBest,\nDonnie",
   kind="email", who="Dr. Patel", subject="EEG lab meeting reschedule", body="Hi Dr. Patel, I wanted to ask if we could move Thursday's lab meeting to Friday at 2 pm. I will bring the updated ear-EEG results and the filter comparison. Best, Donnie"),
 "email_no_headers": dict(text="Here's an email to your landlord:\n\nDear Mr. Lee,\n\nThe kitchen faucet has been leaking since Monday. Could someone come by this week to fix it? I am home after 5 pm every day.\n\nSincerely,\nDonnie",
   kind=("email", "letter"), who="Mr. Lee", subject=None, body="Dear Mr. Lee, The kitchen faucet has been leaking since Monday. Could someone come by this week to fix it? I am home after 5 pm every day. Sincerely, Donnie"),
 "message": dict(text="Here's a text you could send to Sam:\n\n> hey, running about 10 minutes late, go ahead and start without me", kind=("message", "text"), who="Sam", subject=None, body="hey, running about 10 minutes late, go ahead and start without me"),
 "letter": dict(text="Dear Hiring Committee,\n\nI am applying for the research assistant position in the Neural Engineering Lab. Over the past year I built an ear-EEG drowsiness classifier that reached 85 percent accuracy.\n\nSincerely,\nDonnie Brown",
   kind=("letter", "email"), who="Hiring Committee", subject=None, body="Dear Hiring Committee, I am applying for the research assistant position in the Neural Engineering Lab. Over the past year I built an ear-EEG drowsiness classifier that reached 85 percent accuracy. Sincerely, Donnie Brown"),
 "blockquote": dict(text="The paper's key claim:\n\n> Ear-EEG recordings captured drowsiness markers comparable to scalp EEG while remaining comfortable for overnight use.", kind=("quote", "blockquote", "passage"), who=None, subject=None,
   body="Ear-EEG recordings captured drowsiness markers comparable to scalp EEG while remaining comfortable for overnight use."),
 "inline_quote": dict(text='The reviewer wrote: "The filtering pipeline is well motivated, but the cross-validation section needs more detail about subject leakage."', kind=("quote", "passage"), who=None, subject=None,
   body="The filtering pipeline is well motivated, but the cross-validation section needs more detail about subject leakage."),
 "mixed_commentary_email": dict(text="This version is polite but a bit long, so I trimmed it.\n\nSubject: Lab equipment request\n\nHi team,\n\nCould we order two more ear-EEG headsets before the November study? The current pair is booked every day.\n\nThanks,\nDonnie\n\nSend it before noon so purchasing sees it today.",
   kind="email", who=None, subject="Lab equipment request", body="Hi team, Could we order two more ear-EEG headsets before the November study? The current pair is booked every day. Thanks, Donnie", commentary=("trimmed", "noon")),
}
PH = {"bracket": ("Subject: Shipping info\n\nHi Ana,\n\nPlease ship the headset to [mailing address] by Friday.\n\nThanks,\nDonnie", ["mailing address"]),
      "phone": ("Subject: Callback\n\nHi Ana,\n\nYou can reach me at 555-xxxx or xxx-xxxx after 6.\n\nThanks,\nDonnie", ["phone", "number"]),
      "money": ("Subject: Quote\n\nHi Ana,\n\nThe total comes to $XX plus shipping of $X.XX.\n\nThanks,\nDonnie", ["amount", "price", "dollar"]),
      "name_date": ("Subject: Intro\n\nHi [Name],\n\nLet's meet on XX/XX/XXXX at <company> headquarters. My ID is ____.\n\nThanks,\nDonnie", ["name", "date", "company"])}
NEG = {"prose": "The deployment finished on Tuesday after a long delay. Users reported faster page loads within an hour, and the team plans a retrospective next week.",
       "mentions_email": "You should email your professor about the deadline extension, and mention that the dataset was late.",
       "table": "| Plan | Price | Seats |\n|---|---|---|\n| Basic | $9 | 1 |\n| Team | $29 | 5 |",
       "code": "```python\ndef send_email(to, subject):\n    msg = f\"Subject: {subject}\"\n    smtp.send(to, msg)\n```",
       "forum_post": "Re: ALSA crackling on Ubuntu 24.04\n\nPosted by kernel_fan, 3 replies\n\nI fixed it by setting the buffer size to 1024 in pipewire.conf and restarting the service. Hope that helps.",
       "list": "- Gather the raw recordings\n- Clean and re-reference the data\n- Train the classifier",
       "dear_in_prose": "Oh dear, the build failed again because the cache was stale."}
try:
    for nm, c in POS.items():
        out, l, rc = rw(c["text"]); low = out.lower()
        kinds = c["kind"] if isinstance(c["kind"], tuple) else (c["kind"],)
        add(f"pos_{nm}_exit0", rc == 0, rc)
        add(f"pos_{nm}_block_logged", any(k in json.dumps(l.get("verbatim_blocks")).lower() for k in kinds), l.get("verbatim_blocks"))
        add(f"pos_{nm}_kind_named", any(re.search(rf"here'?s (the|a|an) {k}\b|here is (the|a|an) {k}\b", low) for k in kinds), out[:120])
        if c["who"]: add(f"pos_{nm}_recipient_named", c["who"].lower() in low.split(c["body"].lower()[:15])[0] if c["body"].lower()[:15] in low else c["who"].lower() in low, out[:140])
        if c["subject"]: add(f"pos_{nm}_subject_named", f"subject" in low and c["subject"].lower() in low, out[:160])
        r = lcs_ratio(c["body"], out); add(f"pos_{nm}_body_word_fidelity_ge_0.9", r >= 0.9, f"{r:.2f} :: {out[:160]}")
        for w in ("dear", "hi"):
            pass
        if c.get("commentary"): add(f"pos_{nm}_commentary_kept", all(w in low for w in c["commentary"]), out[:200])
        g = BROKEN.search(out); add(f"pos_{nm}_no_broken_clauses", not g, g.group(0) if g else "")
        add(f"pos_{nm}_schema", not validate(l), validate(l))
        off, lo, _ = rw(c["text"], SPEAKEASY_VERBATIM="off")
        add(f"pos_{nm}_off_no_blocks", not lo.get("verbatim_blocks"), lo.get("verbatim_blocks"))
        add(f"pos_{nm}_off_not_verbatim_intro", not re.search(r"here'?s the (email|message|letter|quote)", off.lower()), off[:100])
    for nm, (text, words) in PH.items():
        out, l, rc = rw(text); m = PH_RAW.search(out)
        add(f"ph_{nm}_no_raw_placeholder", not m, f"{m.group(0) if m else ''} :: {out[:160]}")
        add(f"ph_{nm}_spoken_as_placeholder", re.search(r"\bplaceholder\b", out, re.I) and any(w in out.lower() for w in words), out[:160])
        add(f"ph_{nm}_no_broken_clauses", not BROKEN.search(out), (BROKEN.search(out) or [""])[0])
    for nm, text in NEG.items():
        out, l, rc = rw(text)
        add(f"neg_{nm}_no_verbatim_block", not l.get("verbatim_blocks"), l.get("verbatim_blocks"))
        add(f"neg_{nm}_no_verbatim_intro", not re.search(r"here'?s the (email|message|letter|quote|passage)", out.lower()), out[:100])
        add(f"neg_{nm}_schema", not validate(l), validate(l))
    # TTFT: verbatim on vs off, 5 runs, speak with say stub
    tt = {"on": [], "off": []}
    for i in range(5):
        for v in ("on", "off"):
            kw = {} if v == "on" else {"SPEAKEASY_VERBATIM": "off"}; h, e = env(**kw)
            subprocess.run([BIN, "speak"], input=POS["email_headers"]["text"], capture_output=True, text=True, env=e, timeout=200)
            L = jl(os.path.join(h, "logs", "runs.jsonl")); l = flatten(L[-1]) if L else {}
            if isinstance(l.get("tts_first_audio_ms"), (int, float)): tt[v].append(l["tts_first_audio_ms"])
    mon, moff = statistics.median(tt["on"]), statistics.median(tt["off"])
    add("ttft_verbatim_not_slower_than_off_plus_100", mon <= moff + 100, {"on": tt["on"], "off": tt["off"], "median_on": mon, "median_off": moff})
    TT = {"on": tt["on"], "off": tt["off"], "median_on": mon, "median_off": moff}
    # focus mapping of verbatim sentences
    txt = POS["email_headers"]["text"]; lines = txt.split("\n")
    shutil.copy(os.path.join(IMGS, "table.png"), f"/tmp/speakeasy_cap_verb{os.getpid()}.png"); h, e = env(SPEAKEASY_FOCUS="on", STUB_CLIP=txt)
    subprocess.run([BIN, "selection", "--image", f"/tmp/speakeasy_cap_verb{os.getpid()}.png", "--capture-method", "bounds", "--capture-rect", "100,100,800,600"], capture_output=True, text=True, env=e, timeout=200)
    fev = jl(os.path.join(h, "logs", "focus.jsonl")); shows = [x for x in fev if x.get("state") == "show"]; maps = [x for x in fev if x.get("state") == "map"]
    add("focus_verbatim_has_events", len(shows) >= 3, len(shows))
    add("focus_show_range_field_always", shows and all("range" in s for s in shows), [s.get("range") for s in shows][:4])
    def expect_line(t):
        tl = toks(t); best = (0, 0)
        for i, ln in enumerate(lines, 1):
            lt = toks(ln)
            if lt: best = max(best, (len(set(tl) & set(lt)) / len(set(lt)), i))
        return best[1]
    best_rate = {}
    for mode in ("raw", "nonblank"):
        lines = txt.split("\n") if mode == "raw" else [x for x in txt.split("\n") if x.strip()]; hits = []
        for m in maps:
            el = expect_line(m.get("text", "")); ln = m.get("lines") or [None, None]
            if el and ln[0] is not None: hits.append(ln[0] <= el <= ln[1])
        best_rate[mode] = round(sum(hits) / len(hits), 2) if hits else 0
    add("focus_verbatim_sentences_map_to_body_lines", max(best_rate.values()) >= 0.75, f"{best_rate} {[(m.get('lines'), m.get('text','')[:30]) for m in maps][:8]}")
    frag = [m.get("text", "") for m in maps if re.search(r"\b(Dr|Mr|Mrs|Ms|Prof)\.$", m.get("text", "").strip())]
    add("focus_sentence_split_respects_abbreviations", not frag, frag)
    rngs = [s.get("range") for s in shows if s.get("range")]
    def rng_ok(r):
        try:
            a, b = (r["start"], r["end"]) if isinstance(r, dict) else (r[0], r[1]); return 0 <= a < b <= len(txt) + 2
        except Exception: return False
    add("focus_range_values_valid_when_present", all(rng_ok(r) for r in rngs), rngs[:4])
finally:
    for f in os.listdir("/tmp"):
        if f.startswith(f"speakeasy_cap_verb{os.getpid()}"): os.remove(os.path.join("/tmp", f))
res = {"checks": R, "ttft": locals().get("TT"), "summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}}
os.makedirs(os.path.join(AUD, "reports"), exist_ok=True); json.dump(res, open(os.path.join(AUD, "reports", "verbatim.json"), "w"), indent=2, default=str)
md = ["# verbatim tests", f"summary: {res['summary']}", f"ttft: {res['ttft']}", "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:160].replace('|','¦')} |" for c in R]
open(os.path.join(AUD, "reports", "verbatim.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
