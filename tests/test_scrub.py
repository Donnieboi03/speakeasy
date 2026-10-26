"""`speakeasy logs --scrub` (phase7-usable-app.md A5.4, A7 check 9) on fixture logs. Every test runs in a temp
SPEAKEASY_HOME, never ~/.speakeasy."""
import io, json, os, stat, subprocess, sys, tempfile
import pytest
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)
import observe, selog
from focus import _len_and_hash

FORBIDDEN = ("text", "src", "ref_src", "input", "output")
TEXT = "The model reaches 93% accuracy on the held-out set."
UTEXT = "Café results — naïve baseline wins."
SRC_LINES = ["Table 2: accuracy by model", "Ours 93.1 0.12"]
MAP = {"state": "map", "seq": 4, "ts": 1761370000.123, "pid": 41, "sentence_id": 2, "rect": [10.0, 20.0, 300.0, 18.0],
       "text": TEXT, "src": SRC_LINES, "ref_src": ["Earlier line"], "ref_lines": [1, 2], "decision": "ref-back",
       "doc": {"kind": "table", "label": "Accuracy of every model on the held-out set", "page": 3,
               "anchors": [[1.0, 2.0, 3.0, 4.0, "Accuracy of every model"], [5.0, 6.0, 7.0, 8.0]], "target_src": "parse"}}
SHOW = {"state": "show", "seq": 5, "ts": 1761370001.5, "sentence_id": 3, "rect": [1, 2, 3, 4], "text": UTEXT,
        "text_len": 999, "why": "é", "doc": {"kind": "figure", "label": "Figure 3a row 2 (grounded)", "anchors": [[1, 2, 3, 4]]}}
CLEAN = {"state": "show", "seq": 6, "ts": 1761370002.0, "sentence_id": 4, "rect": [1, 2, 3, 4], "text_len": 10,
         "text_hash": "abcdefabcdef", "doc": {"kind": "figure", "label": "Figure 3a", "anchors": [[1, 2, 3, 4]]}}
OLD_FOCUS = {"state": "map", "seq": 1, "ts": 1761360000.0, "sentence_id": 0, "rect": [0, 0, 1, 1], "text": "Old sentence."}
RUN = {"ts": "2026-10-24T10:00:00-0700", "command": "speak", "backend": "llm", "input": "selected text", "output": "spoken text",
       "error": "llm: superlative removed: superlative 'highest' on 0.93 contradicts Accuracy (%), val.; tts: BrokenPipeError",
       "llm_error": "superlative 'lowest' on 12 contradicts Loss", "notes": "superlative removed: superlative 'best' on 1e+06 contradicts F1 score"}
RUN_CLEAN = {"ts": "2026-10-25T10:00:00-0700", "command": "speak", "error": None,
             "notes": "superlative removed: superlative 'best' contradicts its column"}
OLD_RUN = {"ts": "2026-10-20T10:00:00-0700", "command": "rewrite", "input": "old input"}
MALFORMED = b'{"state": "show", "text": "torn'

@pytest.fixture
def logs(monkeypatch):
    home = tempfile.mkdtemp(prefix="speakeasy_scrub_test_")
    monkeypatch.setenv("SPEAKEASY_HOME", home)
    monkeypatch.delenv("SPEAKEASY_LOG_TEXT", raising=False)
    assert selog.home() == home != os.path.expanduser("~/.speakeasy")
    d = os.path.join(home, "logs"); os.makedirs(d)
    focus = [json.dumps(MAP).encode(), json.dumps(SHOW).encode(), json.dumps(CLEAN).encode(), MALFORMED]
    write(d, "focus.jsonl", focus); write(d, "focus.jsonl.1", [json.dumps(OLD_FOCUS).encode()])
    runs = [json.dumps(RUN, ensure_ascii=False).encode(), MALFORMED, json.dumps(RUN_CLEAN, ensure_ascii=False).encode()]
    write(d, "runs.jsonl", runs); write(d, "runs.jsonl.1", [json.dumps(OLD_RUN).encode()])
    write(d, "tracker.jsonl", [b'{"event": "draw", "src": "ax", "seq": 1}', b'{"event": "odd", "text": "hello"}'])
    os.chmod(os.path.join(d, "focus.jsonl"), 0o640)
    return d

def write(d, name, lines):
    with open(os.path.join(d, name), "wb") as f: f.write(b"\n".join(lines) + b"\n")

def snapshot(d):
    out = {}
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n); out[n] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
    return out

def lines(d, name):
    return open(os.path.join(d, name), "rb").read().split(b"\n")[:-1]

def events(d):
    return [json.loads(l) for l in lines(d, "runs.jsonl") if l != MALFORMED and json.loads(l).get("command") == "scrub"]

def test_scrub_rewrites_text_free_and_reports_counts(logs, capsys):
    before = snapshot(logs)
    assert observe.cmd_scrub(yes=True) == 0
    assert sorted(os.listdir(logs)) == sorted(before)                      # no .bak, no temp file
    (ev,) = events(logs)
    assert ev["files"] == 4 and ev["records_changed"] == {"runs": 2, "focus": 3} and ev["other_logs_with_text"] == 1
    assert ev["fields"] == {"text": 3, "src": 1, "ref_src": 1, "doc.label": 1, "doc.anchors": 1, "input": 2, "output": 1,
                            "error": 1, "llm_error": 1, "notes": 1}
    assert set(ev) == {"ts", "command", "files", "records_changed", "fields", "other_logs_with_text", "ms"}

    f = lines(logs, "focus.jsonl")
    assert len(f) == 4 and f[2] == before["focus.jsonl"][0].split(b"\n")[2] and f[3] == MALFORMED
    m, s = json.loads(f[0]), json.loads(f[1])
    assert (m["text_len"], m["text_hash"]) == _len_and_hash(TEXT)
    assert m["src_n"] == 2 and m["src_hash"] == _len_and_hash("\n".join(SRC_LINES))[1]
    assert m["ref_lines"] == [1, 2] and m["doc"]["label"] is None and m["doc"]["anchors"] == [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]
    assert list(m)[:7] == ["state", "seq", "ts", "pid", "sentence_id", "rect", "ref_lines"]   # kept keys keep their order
    for k in ("ts", "state", "seq", "rect", "sentence_id"):
        assert m[k] == MAP[k] and s[k] == SHOW[k]
    assert s["text_len"] == 999 and s["text_hash"] == _len_and_hash(UTEXT)[1]  # present keys are not overwritten
    assert s["doc"]["label"] == "Figure 3a row 2 (grounded)"                  # a figure label stays
    assert b"\\u00e9" in f[1]                                                 # focus.py's writer escapes non-ASCII
    assert json.loads(lines(logs, "focus.jsonl.1")[0]) == {**{k: v for k, v in OLD_FOCUS.items() if k != "text"},
                                                            "text_len": 13, "text_hash": _len_and_hash("Old sentence.")[1]}

    r = lines(logs, "runs.jsonl")
    assert len(r) == 4 and r[1] == MALFORMED and r[2] == before["runs.jsonl"][0].split(b"\n")[2]
    run = json.loads(r[0])
    assert list(run) == ["ts", "command", "backend", "error", "llm_error", "notes"]
    assert run["error"] == "llm: superlative removed: superlative 'highest' contradicts its column; tts: BrokenPipeError"
    assert run["llm_error"] == "superlative 'lowest' contradicts its column"
    assert run["notes"] == "superlative removed: superlative 'best' contradicts its column"
    assert json.loads(lines(logs, "runs.jsonl.1")[0]) == {"ts": OLD_RUN["ts"], "command": "rewrite"}

    for name in ("focus.jsonl", "focus.jsonl.1", "runs.jsonl", "runs.jsonl.1"):
        for l in lines(logs, name):
            if l == MALFORMED: continue
            rec = json.loads(l)
            assert not any(k in rec for k in FORBIDDEN), (name, rec)
            assert " on " not in str(rec.get("error")) + str(rec.get("llm_error")) + str(rec.get("notes"))
    assert stat.S_IMODE(os.stat(os.path.join(logs, "focus.jsonl")).st_mode) == 0o640
    assert snapshot(logs)["tracker.jsonl"] == before["tracker.jsonl"]
    out = capsys.readouterr().out
    assert "Scrubbed 5 records in 4 files" in out and TEXT not in out and logs not in out

def test_second_run_changes_nothing(logs):
    assert observe.cmd_scrub(yes=True) == 0
    first = snapshot(logs)
    assert observe.cmd_scrub(yes=True) == 0
    after = snapshot(logs)
    ev = events(logs)[-1]
    assert ev["files"] == 0 and ev["records_changed"] == {"runs": 0, "focus": 0} and not any(ev["fields"].values())
    for name in first:
        if name != "runs.jsonl": assert after[name] == first[name], name     # same bytes and mtime
    assert after["runs.jsonl"][0].startswith(first["runs.jsonl"][0])       # only the count event was appended
    assert len(lines(logs, "runs.jsonl")) == len(first["runs.jsonl"][0].split(b"\n"))

@pytest.mark.parametrize("target", ["replace", "dumps"])
def test_write_failure_leaves_originals(logs, monkeypatch, capsys, target):
    before = snapshot(logs)
    def boom(*a, **k): raise OSError("injected")
    if target == "replace": monkeypatch.setattr(observe.os, "replace", boom)
    else: monkeypatch.setattr(observe.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("injected")))
    assert observe.cmd_scrub(yes=True) == 1
    assert snapshot(logs) == before                                        # byte-identical, no temp file, no event
    assert ("OSError" if target == "replace" else "TypeError") in capsys.readouterr().err

def test_live_read_refuses(logs, capsys):
    with open(selog.pidfile(), "w") as f: f.write(str(os.getpid()))
    before = snapshot(logs)
    assert observe.cmd_scrub(yes=True) == 2
    assert capsys.readouterr().err.strip() == "A read is running. Run the scrub after it ends."
    assert snapshot(logs) == before

def test_stale_pid_does_not_block(logs):
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    with open(selog.pidfile(), "w") as f: f.write(str(p.pid))
    assert observe.cmd_scrub(yes=True) == 0 and events(logs)

@pytest.mark.parametrize("answer, code", [("n\n", 1), ("\n", 1), ("", 1), ("yes\n", 0)])
def test_prompt(logs, monkeypatch, capsys, answer, code):
    before = snapshot(logs)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert observe.cmd_scrub() == code
    assert "Remove text from 5 records in 4 files? [y/N] " in capsys.readouterr().out
    if code: assert snapshot(logs) == before
    else: assert len(events(logs)) == 1

def test_nothing_to_scrub_still_logs_the_event(logs, capsys):
    for n in os.listdir(logs): os.remove(os.path.join(logs, n))
    write(logs, "runs.jsonl", [json.dumps(RUN_CLEAN, ensure_ascii=False).encode()])
    assert observe.cmd_scrub() == 0                                         # no prompt when there is nothing to do
    assert "Nothing to scrub." in capsys.readouterr().out
    assert events(logs)[0]["records_changed"] == {"runs": 0, "focus": 0}

def test_log_text_on_warns_and_scrubs(logs, monkeypatch, capsys):
    monkeypatch.setenv("SPEAKEASY_LOG_TEXT", "1")
    assert observe.cmd_scrub(yes=True) == 0
    assert "SPEAKEASY_LOG_TEXT=1 is set: new records will hold text again." in capsys.readouterr().out
    assert events(logs)[0]["records_changed"] == {"runs": 2, "focus": 3}

def test_cli(logs):
    home = os.path.dirname(logs)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPEAKEASY_")}
    env.update(SPEAKEASY_HOME=home, HOME=home, TMPDIR=home, SPEAKEASY_TRACK="off")
    cli = [sys.executable, os.path.join(SRC, "speakeasy.py"), "logs"]
    p = subprocess.run(cli, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "usage: speakeasy logs --scrub [--yes]" in p.stderr
    p = subprocess.run(cli + ["--scrub", "--yes"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert events(logs)[0]["records_changed"] == {"runs": 2, "focus": 3}
