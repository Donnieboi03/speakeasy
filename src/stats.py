"""Usage stats from runs.jsonl (counts only; no text is ever stored or read)."""
import datetime, statistics
from collections import Counter

READ_WPM = 238          # average silent reading speed for adult non-fiction (Brysbaert 2019); used only for the estimate

def kind_of(rec):
    """figure | table | list | text for one run record."""
    vb = rec.get("verbatim_blocks") or []
    if rec.get("image") or rec.get("figures_found"): return "figure"
    if "table" in vb: return "table"
    if "list" in vb: return "list"
    return "text"

def words(s):
    return len((s or "").split())

def compute(runs, today=None):
    today = today or datetime.date.today()
    reads = [r for r in runs if r.get("command") in ("speak", "selection") and not r.get("dry_run")]
    per_day = Counter()
    for r in reads:
        try: d = datetime.date.fromisoformat(str(r.get("ts", ""))[:10])
        except ValueError: continue
        per_day[d.isoformat()] += int(r.get("words_spoken") or 0)
    days = sorted(d for d, w in per_day.items() if w > 0)
    longest = cur = 0; prev = None
    for d in days:
        dd = datetime.date.fromisoformat(d)
        cur = cur + 1 if prev and (dd - prev).days == 1 else 1
        longest = max(longest, cur); prev = dd
    streak = 0; d = today
    if d.isoformat() not in per_day: d -= datetime.timedelta(days=1)   # today not read yet: the streak still counts
    while per_day.get(d.isoformat()): streak += 1; d -= datetime.timedelta(days=1)
    kinds = Counter(r.get("kind") or kind_of(r) for r in reads)
    fa = [(r.get("timings") or {}).get("tts_first_audio_ms") for r in reads]
    fa = [v for v in fa if isinstance(v, (int, float))]
    w_in = sum(int(r.get("words_in") or 0) for r in reads)
    w_sp = sum(int(r.get("words_spoken") or 0) for r in reads)
    return {
        "reads": len(reads), "words_in": w_in, "words_listened": w_sp,
        "audio_minutes": round(sum(float(r.get("audio_s") or 0) for r in reads) / 60, 1),
        "figures_explained": kinds.get("figure", 0), "tables_read": kinds.get("table", 0),
        "lists_read": kinds.get("list", 0),
        "median_first_audio_ms": round(statistics.median(fa)) if fa else None,
        "time_saved_min_estimate": round(w_in / READ_WPM, 1), "time_saved_basis": f"words in / {READ_WPM} wpm (estimate)",
        "streak_days": streak, "longest_streak_days": longest,
        "today_words": per_day.get(today.isoformat(), 0),
        "days": {d: per_day[d] for d in sorted(per_day)},
    }
