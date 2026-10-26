#!/usr/bin/env python3
"""Build every spec of a set into fixtures and write the manifest; --check regenerates into a temp dir and diffs.

  make_fixtures.py [--set public|hidden] [--holdout DIR] [--check] [--only DOC_ID]

public: specs/*.json -> fixtures/public/<doc_id>/, manifest fixtures/public/manifest.json
hidden: <holdout>/specs/*.json -> <holdout>/fixtures/<doc_id>/, manifest <holdout>/manifest.json
Synthetic specs (with "flow") go through gen/synth.py; real specs ("source": "arxiv") through gen/snap_gt.py, which reads the
committed, unmodified doc.pdf (downloaded from the spec's url when missing, then checked against pdf_sha256)."""
import argparse, filecmp, hashlib, json, os, shutil, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__)); BENCH = os.path.dirname(HERE)
FILES = ("doc.pdf", "gt.json", "cases.json")


def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()


def paths(set_name, holdout):
    if set_name == "public": return os.path.join(BENCH, "specs"), os.path.join(BENCH, "fixtures", "public"), os.path.join(BENCH, "fixtures", "public", "manifest.json")
    return os.path.join(holdout, "specs"), os.path.join(holdout, "fixtures"), os.path.join(holdout, "manifest.json")


def manifest(set_name, fx, doc_ids):
    docs = []
    for d in sorted(doc_ids):
        gt = json.load(open(os.path.join(fx, d, "gt.json")))
        docs.append({"doc_id": d, "subset": gt["subset"], "sha256": {f: sha(os.path.join(fx, d, f)) for f in FILES}})
    body = json.dumps(docs, sort_keys=True, separators=(",", ":"))
    return {"schema": "speakeasy-bench-manifest/1", "set": set_name, "docs": docs, "manifest_sha256": hashlib.sha256(body.encode()).hexdigest()}


def build_one(spec_path, outdir, committed_pdf=None):
    sys.path.insert(0, HERE)
    spec = json.load(open(spec_path, encoding="utf-8")); os.makedirs(outdir, exist_ok=True)
    if spec.get("source") == "arxiv":
        import snap_gt
        dst = os.path.join(outdir, "doc.pdf")
        src = committed_pdf if committed_pdf and os.path.exists(committed_pdf) else None
        if src and os.path.abspath(src) != os.path.abspath(dst): shutil.copyfile(src, dst)
        if not os.path.exists(dst):
            req = urllib.request.Request(spec["url"], headers={"User-Agent": "speakeasy-bench/1"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f: f.write(r.read())
        if sha(dst) != spec["pdf_sha256"]: raise SystemExit(f"{spec['doc_id']}: downloaded PDF sha256 differs from pdf_sha256")
        if os.path.getsize(dst) > 8 * 1024 * 1024: raise SystemExit(f"{spec['doc_id']}: PDF larger than 8 MB")
        snap_gt.build(spec_path, outdir)
    else:
        import synth
        synth.build(spec_path, outdir)
    return spec["doc_id"]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--set", default="public", choices=("public", "hidden"))
    ap.add_argument("--holdout", default=os.environ.get("SPEAKEASY_HOLDOUT", os.path.expanduser("~/speakeasy-holdout/bench")))
    ap.add_argument("--check", action="store_true"); ap.add_argument("--only")
    a = ap.parse_args()
    import importlib.util
    if importlib.util.find_spec("reportlab") is None:
        print("reportlab not importable", file=sys.stderr); sys.exit(2)
    specs_dir, fx, man = paths(a.set, a.holdout)
    if not os.path.isdir(specs_dir): print(f"no specs dir {specs_dir}", file=sys.stderr); sys.exit(2)
    specs = sorted(os.path.join(specs_dir, f) for f in os.listdir(specs_dir) if f.endswith(".json"))
    if a.check:
        tmp = tempfile.mkdtemp(prefix="se_fxcheck_"); drift = []
        try:
            ids = []
            for sp in specs:
                d = json.load(open(sp))["doc_id"]; ids.append(d)
                if a.only and d != a.only: continue
                build_one(sp, os.path.join(tmp, d), committed_pdf=os.path.join(fx, d, "doc.pdf"))
                for f in FILES:
                    c = os.path.join(fx, d, f)
                    if not os.path.exists(c) or not filecmp.cmp(c, os.path.join(tmp, d, f), shallow=False): drift.append(f"{d}/{f}")
            if not a.only and os.path.exists(man):
                if json.load(open(man)) != manifest(a.set, fx, ids): drift.append("manifest.json")
            elif not a.only: drift.append("manifest.json (missing)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if drift: print("fixture drift: " + ", ".join(drift), file=sys.stderr); sys.exit(1)
        print(f"make_fixtures --check: {a.set} {len(specs)} specs, no drift"); return
    os.makedirs(fx, exist_ok=True); ids = []
    for sp in specs:
        d = json.load(open(sp))["doc_id"]; ids.append(d)
        if a.only and d != a.only: continue
        build_one(sp, os.path.join(fx, d), committed_pdf=os.path.join(fx, d, "doc.pdf")); print("built", d)
    m = manifest(a.set, fx, ids)
    with open(man, "w") as f: json.dump(m, f, indent=1); f.write("\n")
    print(f"manifest {man} {m['manifest_sha256'][:12]} ({len(ids)} docs)")


if __name__ == "__main__":
    main()
