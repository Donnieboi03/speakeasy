#!/usr/bin/env python3
"""Audit a focus.jsonl (+ optional runs.jsonl): event ordering, rect bounds, clears, map coverage.
Usage: audit_focus.py FOCUS_JSONL [--runs RUNS_JSONL] [--rect x,y,w,h]"""
import json, sys, argparse
def load(p): return [json.loads(l) for l in open(p) if l.strip()]
def audit(events, rect=None):
    probs, shows, maps, seqs = [], 0, 0, []
    last_map = None; cleared = {}
    for i, e in enumerate(events):
        st = e.get("state"); pid = e.get("pid")
        if st == "map": maps += 1; last_map = e
        elif st == "show":
            shows += 1; seqs.append(e.get("seq"))
            if pid in cleared: probs.append(f"#{i} show after clear (pid {pid})")
            r = e.get("rect")
            if not (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)): probs.append(f"#{i} bad rect {r}")
            else:
                if r[2] <= 0 or r[3] <= 0: probs.append(f"#{i} empty rect {r}")
                if rect:
                    x, y, w, h = rect
                    if r[0] < x - 1 or r[1] < y - 1 or r[0] + r[2] > x + w + 1 or r[1] + r[3] > y + h + 1: probs.append(f"#{i} rect {r} outside capture {rect}")
            if (e.get("expires") or 0) <= (e.get("ts") or 0): probs.append(f"#{i} expires before ts")
            if not last_map or last_map.get("pid") != pid: probs.append(f"#{i} show without map")
            if "[" in str(e.get("text", "")) or "]" in str(e.get("text", "")): probs.append(f"#{i} bracket in focus text")
        elif st == "clear": cleared[pid] = e.get("ts")
        else: probs.append(f"#{i} unknown state {st}")
    if seqs != sorted(seqs): probs.append("seq not increasing")
    if shows and not cleared: probs.append("no clear at end")
    methods = {}
    for e in events:
        if e.get("state") == "map": methods[e.get("method")] = methods.get(e.get("method"), 0) + 1
    return {"events": len(events), "maps": maps, "shows": shows, "clears": len(cleared), "methods": methods, "problems": probs}
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("path"); ap.add_argument("--rect"); a = ap.parse_args()
    print(json.dumps(audit(load(a.path), [float(v) for v in a.rect.split(",")] if a.rect else None), indent=2))
