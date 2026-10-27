#!/usr/bin/env python3
"""Targeted Phase 6 fixtures for run_phase6.py, generated per run into its temp root: make_p6docs.py OUTDIR [REPO]
Builds <OUTDIR>/<name>/{doc.pdf, gt.json, cases.json} with the verifier's gap generator (tests/audit/bench/gen/gap_synth.py) and
writes <OUTDIR>/p6_docs.json listing them. Needs reportlab + Pillow (the test venv)."""
import json, os, sys
OUT = os.path.abspath(sys.argv[1]); REPO = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tests", "audit", "bench", "gen"))
import gap_synth
A4 = [595.3, 841.9]
P = lambda pid, y, s, **kw: dict({"para": pid, "rect": [56, y, 539], "s": s}, **kw)
DOCS = {
 # 14.7 lexicon positives, each on a body of the noun's kind (Exhibit on both kinds), plus Roman numerals and the ambiguous nouns
 "lexpos": {"page": A4, "size": 10, "pages": [
   {"items": [
    P("p1", 50, ["Every caption on this page uses a less common form.", "Each one sits right under or over its body."]),
    {"figure": "F1", "body": "bars", "rect": [56, 96, 290, 230], "data": [4, 7, 5], "gap": "lexicon", "caption": {"text": ["Exhibit 3: Loads by depot."], "style": "exhibit", "label": "Exhibit 3"}},
    {"table": "T1", "style": "ruled", "rect": [320, 96, 539], "size": 8, "gap": "lexicon", "rows": [["A", "B"], ["1", "2"], ["3", "4"]],
     "caption": {"text": ["Exhibit 4: Staff per shift."], "style": "exhibit", "label": "Exhibit 4"}},
    {"figure": "F2", "body": "line", "rect": [56, 280, 290, 410], "data": [2, 4, 3, 6], "gap": "lexicon", "caption": {"text": ["Abb. 2: Verlauf der Last."], "style": "abb", "label": "Abb. 2"}},
    {"table": "T2", "style": "grid", "rect": [320, 280, 539], "size": 8, "gap": "lexicon", "rows": [["Ort", "Wert"], ["Nord", "12"], ["Süd", "9"]],
     "caption": {"text": ["Tabelle 1. Werte je Ort."], "style": "tabelle", "label": "Tabelle 1"}},
    {"table": "T3", "style": "ruled", "rect": [56, 470, 290], "size": 8, "gap": "lexicon", "rows": [["Item", "Qty"], ["Bolts", "20"], ["Nuts", "40"]],
     "caption": {"text": ["TABLE IV", "Parts on hand."], "break": True, "style": "table-roman", "label": "TABLE IV"}},
    {"figure": "F3", "body": "scatter", "rect": [320, 470, 539, 600], "seed": 3, "gap": "lexicon", "caption": {"text": ["Fig. S2. Drift over the trial."], "style": "fig-s", "label": "Fig. S2"}},
    {"figure": "F4", "body": "pie", "rect": [56, 650, 290, 780], "data": [5, 3, 2], "gap": "lexicon", "caption": {"text": ["Figura 3 - Reparto de casos."], "style": "figura", "label": "Figura 3"}},
    {"figure": "F5", "body": "bars", "rect": [320, 650, 539, 780], "data": [3, 6, 4], "gap": "lexicon", "caption": {"text": ["Chart 1: Visits per month."], "style": "chart", "label": "Chart 1"}}]},
   {"items": [
    P("p2", 50, ["The second page holds decimal, appendix and Roman numbers.", "It also holds the ambiguous nouns."]),
    {"figure": "F6", "body": "line", "rect": [56, 96, 290, 230], "data": [5, 6, 8, 7], "gap": "lexicon", "caption": {"text": ["Figure 3.2: Load per hour."], "style": "figure", "label": "Figure 3.2"}},
    {"table": "T4", "style": "grid", "rect": [320, 96, 539], "size": 8, "gap": "lexicon", "rows": [["Key", "Val"], ["x", "1"], ["y", "2"]],
     "caption": {"text": ["TABLE XL", "Last Roman number."], "break": True, "style": "table-roman", "label": "TABLE XL"}},
    {"table": "T5", "style": "ruled", "rect": [56, 290, 290], "size": 8, "gap": "lexicon", "rows": [["Posten", "Euro"], ["Miete", "900"], ["Strom", "120"]],
     "caption": {"text": ["Anlage 1: Kosten."], "style": "anlage", "label": "Anlage 1"}},
    {"figure": "F7", "body": "photo", "rect": [320, 290, 539, 420], "seed": 5, "gap": "lexicon", "caption": {"text": ["Anexo 2: Foto del sitio."], "style": "anexo", "label": "Anexo 2"}},
    {"figure": "F8", "body": "bars", "rect": [56, 480, 290, 610], "data": [2, 5, 3], "gap": "lexicon", "caption": {"text": ["Fig. II: Second view."], "style": "fig-roman", "label": "Fig. II"}},
    {"table": "T6", "style": "ruled", "rect": [320, 480, 539], "size": 8, "gap": "lexicon", "rows": [["Code", "Count"], ["A1", "3"], ["B2", "5"]],
     "caption": {"text": ["Table A.1: Appendix counts."], "style": "table", "label": "Table A.1"}}]}],
  "cases": [{"id": "c01", "select": "p1"}, {"id": "c02", "select": "p2"}]},
 # 14.7 lexicon negatives: prose that must stay prose, some right next to a body
 "lexneg": {"page": A4, "size": 10, "pages": [{"items": [
    P("p1", 56, ["Table 2 shows the totals for each depot.", "The totals are drawn from the shift logs."], decoy=True),
    {"table": "T1", "style": "ruled", "rect": [100, 96, 495], "size": 8, "rows": [["Depot", "Total"], ["North", "40"], ["Quay", "31"]]},
    P("p2", 190, ["Chart 1 of the plan lists every new route.", "Each route has a cost and a distance."], decoy=True),
    {"figure": "F1", "body": "bars", "rect": [100, 230, 495, 380], "data": [3, 5, 4]},
    {"text": "TABLE OF CONTENTS", "at": [56, 410], "size": 11, "kind": "heading", "decoy": True},
    P("p3", 436, ["Figure skating is popular in the north.", "Many drivers coach at the weekend."], decoy=True),
    P("p4", 490, ["Exhibit A lists the leases that end this year.", "The rest end later."], decoy=True),
    P("p5", 544, ["Abbildung 3 zeigt den Verlauf der Messung.", "Die Werte steigen am Morgen."], decoy=True)]}],
  "cases": [{"id": "c01", "select": "p3"}]},
 # uncaptioned bodies next to standard captions and decoys on one report page
 "uncap": {"page": A4, "size": 10, "pages": [{"items": [
    {"decoy": "logo", "rect": [430, 14, 539, 46], "word": "NORDA"},
    P("p1", 56, ["Deliveries rose in four of the five months.", "The harbour site carried most of the load."]),
    {"figure": "F1", "body": "bars", "rect": [100, 96, 495, 250], "data": [12, 18, 9, 21]},
    P("p2", 266, ["Staffing stayed flat across the quarter.", "Overtime fell in the last month."]),
    {"table": "T1", "style": "ruled", "rect": [100, 306, 495], "size": 9, "rows": [["Site", "Trucks", "Staff"], ["Harbour", "14", "38"], ["Ridge", "9", "21"], ["Mill", "6", "15"]]},
    {"figure": "F2", "body": "line", "rect": [100, 420, 495, 560], "data": [3, 5, 4, 7], "gap": "standard",
     "caption": {"text": ["Figure 1: Weekly loads at the harbour."], "style": "standard", "label": "Figure 1"}},
    {"table": "T2", "style": "grid", "rect": [100, 610, 495], "size": 9, "gap": "standard", "rows": [["Week", "Loads"], ["1", "40"], ["2", "46"]],
     "caption": {"text": ["Table 1: Loads per week."], "style": "standard", "label": "Table 1"}},
    {"decoy": "text-image", "rect": [100, 720, 495, 790], "s": ["This box is a picture of text.", "It must never become a figure."]}]}],
  "cases": [{"id": "c01", "select": "p1"}, {"id": "c02", "select": "p2"}]},
 # A2.3 header tie on an OCR-row table: an image table (no text layer in its body) read through a fake OCR helper
 "ocrtie": {"page": A4, "size": 10, "pages": [{"items": [
    P("p1", 56, ["The depot sheet below was pasted in as a picture.", "Its rows come from OCR only."]),
    {"table": "T1", "style": "ruled", "image": True, "rect": [110, 110, 485], "size": 10, "gap": "image-table",
     "rows": [["Depot", "Crates", "Pallets"], ["North", "42", "12"], ["Quay", "37", "9"], ["Inland", "55", "16"]],
     "caption": {"text": ["Table 3: Stock by depot."], "style": "standard", "label": "Table 3"}},
    P("p2", 300, ["Inland holds the most crates.", "Quay is due a restock."])]}],
  "cases": [{"id": "c01", "select": "p1"},
            # A2.3 ruling (Chat as Planner): the tie rule applies among rows above the existing row-match threshold (0.4 of a row's
            # tokens), so the tie is 2 vs 2 (header crates+pallets, Quay row quay+37: 2/3 each), not 1 vs 1 (1/3, whole-table cue)
            {"id": "r01", "select": "T1:caption", "script": [{"text": "Depot, Crates and Pallets head the columns.", "row": 0},
                                                           {"text": "Crates and Pallets at Quay: 37.", "row": 2}]}]},
}
os.makedirs(OUT, exist_ok=True); listing = {}
for name, spec in DOCS.items():
    spec = dict(spec, doc_id=f"p6-{name}"); sp = os.path.join(OUT, f"{name}.spec.json"); json.dump(spec, open(sp, "w"), ensure_ascii=False)
    gap_synth.build(sp, os.path.join(OUT, name)); listing[name] = os.path.join(OUT, name)
json.dump(listing, open(os.path.join(OUT, "p6_docs.json"), "w"), indent=1)
print(json.dumps(listing))
