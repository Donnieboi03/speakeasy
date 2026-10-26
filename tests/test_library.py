"""Document library (phase7-usable-app.md A5.3): library.json is written by the backend after it resolves a document,
mode 0600, keyed by the run's doc_path_hash; names and paths never reach a log; names off deletes the file."""
import glob, json, os, stat, subprocess, sys, tempfile
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
FIXTURE = os.path.join(os.path.dirname(__file__), "audit", "bench", "fixtures", "public", "syn-panels-01", "doc.pdf")

def tiny_pdf(path, title_obj):
    body = (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n"
            b"3 0 obj << /Title (Chapter outline) >> endobj\n7 0 obj << " + title_obj + b" /Producer (x) >> endobj\n"
            b"trailer << /Root 1 0 R /Info 7 0 R >>\n%%EOF\n")
    with open(path, "wb") as f: f.write(body)

def test_pdf_title_reads_the_info_dictionary_only():
    import document
    d = tempfile.mkdtemp()
    tiny_pdf(os.path.join(d, "a.pdf"), rb"/Title (Attention \(and\) figures)")
    assert document.pdf_title(os.path.join(d, "a.pdf")) == "Attention (and) figures"
    tiny_pdf(os.path.join(d, "b.pdf"), b"/Title <FEFF004F006E0020004600690067>")
    assert document.pdf_title(os.path.join(d, "b.pdf")) == "On Fig"
    tiny_pdf(os.path.join(d, "c.pdf"), b"/Title ()")
    assert document.pdf_title(os.path.join(d, "c.pdf")) is None                 # empty: the file name is used instead
    tiny_pdf(os.path.join(d, "d.pdf"), b"/Title (" + b"x" * 130 + b")")
    assert document.pdf_title(os.path.join(d, "d.pdf")) is None                 # 120 characters or more: file name
    assert document.url_name("https://arxiv.org/pdf/2402.10153v2.pdf?x=1") == "2402.10153v2.pdf"

def test_note_library_mode_update_and_off(monkeypatch):
    import document
    home = tempfile.mkdtemp(); monkeypatch.setenv("SPEAKEASY_HOME", home); monkeypatch.delenv("SPEAKEASY_DOC_NAMES", raising=False)
    document.note_library("abc123abc123", "/tmp/x.pdf", "X")
    lp = os.path.join(home, "library.json")
    first = json.load(open(lp))["abc123abc123"]
    assert stat.S_IMODE(os.stat(lp).st_mode) == 0o600 and first["name"] == "X" and first["path"] == "/tmp/x.pdf"
    document.note_library("abc123abc123", "/tmp/x.pdf", "X2")
    again = json.load(open(lp))["abc123abc123"]
    assert again["first_ts"] == first["first_ts"] and again["name"] == "X2"
    assert not [n for n in os.listdir(home) if n.startswith(".library_")]      # no temp file left
    monkeypatch.setenv("SPEAKEASY_DOC_NAMES", "off")
    document.note_library("ffffffffffff", "/tmp/y.pdf", "Y")
    assert not os.path.exists(lp)

def test_read_adds_one_entry_and_logs_hold_no_name_or_path():
    home = tempfile.mkdtemp(); hint = os.path.join(home, "hint.json")
    json.dump({"app": "Preview", "ax_document": "file://" + os.path.abspath(FIXTURE), "title": "doc.pdf"}, open(hint, "w"))
    env = dict(os.environ, SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1", SPEAKEASY_PREWARM="0", SPEAKEASY_LAYOUT="off")
    env.pop("SPEAKEASY_DOC_NAMES", None); env.pop("SPEAKEASY_LOG_TEXT", None)
    p = subprocess.run([sys.executable, os.path.join(SRC, "speakeasy.py"), "speak", "--dry-run", "--doc-hint", hint], input="Panel text of the figure.",
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    run = json.loads(open(os.path.join(home, "logs", "runs.jsonl")).readlines()[-1])
    lib = json.load(open(os.path.join(home, "library.json")))
    assert list(lib) == [run["doc_path_hash"]], (list(lib), run.get("doc_path_hash"), run.get("doc_note"))
    entry = lib[run["doc_path_hash"]]
    assert entry["path"] == os.path.abspath(FIXTURE) and entry["name"]
    for f in glob.glob(os.path.join(home, "logs", "*")):
        data = open(f, encoding="utf-8", errors="replace").read()
        assert entry["path"] not in data and (len(entry["name"]) < 8 or entry["name"] not in data), f
