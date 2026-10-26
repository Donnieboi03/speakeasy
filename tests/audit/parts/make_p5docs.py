#!/usr/bin/env python3
"""Synthetic PDFs for the phase-5 holdout suite (our own content; reportlab). Usage: make_p5docs.py OUTDIR
Writes callouts.pdf, panels.pdf, single.pdf and p5_gt.json (ground truth in page points, top-left origin, x0,y0,x1,y1).

callouts.pdf  p1 Figure 1: callout markers of every kind (enclosed circle, circled glyph, leader, two-segment leader into a
              nested object, enclosed but unattached) plus axis tick labels 1-4 with tick marks 6 pt away (never callouts).
              p2 Figure 2: drawn (a)/(b) panels; callout 1 in both panels, callout 3 only in (b).
panels.pdf    one figure per page, no drawn panel labels: 2 columns (gutter 30 pt), 2x2 grid, 3 columns with a caption naming 2,
              2 columns with a 4 pt gap, 2 rows, 2 columns joined by an arrow across the gutter.
single.pdf    one figure with a single graphic object (fewer than 2 pick candidates)."""
import json, os, sys
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

W, H = letter
FONT = os.environ.get("P5_DEJAVU", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bench", "fonts", "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu", FONT))
GT = {}


def r1(v): return [round(x, 1) for x in v]


class Page:
    def __init__(self, c): self.c = c

    def rect(self, x0, y0, x1, y1, lw=1.0, fill=False):
        self.c.setLineWidth(lw); self.c.rect(x0, H - y1, x1 - x0, y1 - y0, stroke=1, fill=1 if fill else 0); return [x0, y0, x1, y1]

    def circle(self, cx, cy, r, lw=0.8):
        self.c.setLineWidth(lw); self.c.circle(cx, H - cy, r, stroke=1, fill=0); return [cx - r, cy - r, cx + r, cy + r]

    def line(self, pts, lw=0.8):
        self.c.setLineWidth(lw); p = self.c.beginPath(); p.moveTo(pts[0][0], H - pts[0][1])
        for x, y in pts[1:]: p.lineTo(x, H - y)
        self.c.drawPath(p, stroke=1, fill=0)

    def text(self, x, base, s, font="Helvetica", size=9):
        self.c.setFont(font, size); self.c.drawString(x, H - base, s)
        w = pdfmetrics.stringWidth(s, font, size); return [x, base - 0.75 * size, x + w, base + 0.2 * size]

    def ctext(self, cx, cy, s, font="Helvetica", size=8):
        w = pdfmetrics.stringWidth(s, font, size); base = cy + 0.35 * size
        return self.text(cx - w / 2, base, s, font, size)

    def prose(self, y, lines, size=10):
        for ln in lines: self.text(72, y, ln, size=size); y += 14
        return y


def callouts(path):
    c = canvas.Canvas(path, pagesize=letter); P = Page(c); g = {"p1": {}, "p2": {}}
    P.prose(60, ["The treatment line has five sensors along the water path.", "Each sensor is numbered in the drawing below.",
                 "The flow chart under the drawing gives the share of each stage."])
    o = g["p1"]
    o["O1"] = P.rect(80, 170, 150, 210); P.text(90, 193, "Intake")
    o["M1"] = P.circle(165, 180, 6); P.ctext(165, 180, "1")                       # enclosed, 9 pt from O1 -> attached O1
    o["O2"] = P.rect(200, 170, 280, 210); P.text(206, 193, "Sand filter")
    o["M2"] = P.text(290, 186, "\u2461", font="DejaVu", size=10)                  # circled glyph two, 10 pt from O2
    o["O3"] = P.rect(330, 250, 380, 290)                                           # leader far end target
    o["M3"] = P.text(420, 178, "3")                                                # plain number with a leader
    P.line([(418.5, 176), (360, 270)])
    o["O4"] = P.rect(430, 165, 470, 195)                                           # near the marker, NOT the target
    o["M4"] = P.text(500, 242, "4")
    P.line([(498.5, 240), (482, 262), (470, 300)])                                 # two segments, far end inside O5
    o["O6"] = P.rect(440, 280, 540, 340); o["O5"] = P.rect(460, 292, 480, 308)
    o["M5"] = P.rect(95, 300, 107, 312, lw=0.8); P.ctext(101, 306, "5")           # enclosed square, nothing within 14 pt
    o["tank"] = P.rect(200, 300, 300, 340); P.text(215, 324, "Clear well")
    # flow chart: axis with tick marks; tick labels 1..4 sit 6 pt below the tick ends (never callouts)
    P.line([(80, 370), (320, 370)]); o["ticks"] = []
    for i, x in enumerate((110, 170, 230, 290)):
        P.line([(x, 370), (x, 374)], lw=0.6); o["ticks"].append(P.ctext(x, 384.5, str(i + 1), size=8))
    for i, (x, h) in enumerate(((100, 20), (160, 34), (220, 12), (280, 26))): P.rect(x, 368 - h, x + 20, 368, lw=0.5, fill=True)
    o["caption"] = P.text(72, 420, "Figure 1: Treatment line with numbered sensors and a flow chart of the stages.")
    P.prose(450, ["Callout 3 is the turbidity probe after the sand filter.", "The flow chart shows the share of each stage."])
    c.showPage()
    P = Page(c); o = g["p2"]
    P.prose(60, ["The second figure splits the line into two panels.", "Valves carry the same number in both panels."])
    o["Fa"] = P.rect(60, 180, 300, 350, lw=0.6); o["Fb"] = P.rect(320, 180, 560, 350, lw=0.6)
    P.text(64, 192, "(a)", font="Helvetica-Bold", size=10); P.text(324, 192, "(b)", font="Helvetica-Bold", size=10)
    o["Pa"] = P.rect(100, 200, 180, 260); P.text(110, 233, "Stage one")
    o["Ma1"] = P.circle(194, 210, 6); P.ctext(194, 210, "1")
    o["Pa2"] = P.rect(210, 280, 280, 320)
    o["Pb1"] = P.rect(360, 200, 440, 260); P.text(370, 233, "Stage two")
    o["Mb1"] = P.circle(454, 210, 6); P.ctext(454, 210, "1")
    o["Pb2"] = P.rect(470, 280, 540, 320)
    o["Mb3"] = P.circle(505, 336, 6); P.ctext(505, 336, "3")
    o["caption"] = P.text(72, 370, "Figure 2: Two stages. (a) Stage one with its valve. (b) Stage two with its valve and drain.")
    P.prose(400, ["The drain only exists in the second stage."])
    c.showPage(); c.save(); GT["callouts"] = g


def cell(P, x0, y0, x1, y1, name):
    """A framed panel cell with two overlapping labelled boxes (no clear band inside the frame)."""
    w = x1 - x0; h = y1 - y0
    P.rect(x0, y0, x1, y1, lw=0.6)
    P.rect(x0 + 0.08 * w, y0 + 0.12 * h, x0 + 0.62 * w, y0 + 0.55 * h); P.text(x0 + 0.1 * w, y0 + 0.3 * h, name + " in")
    P.rect(x0 + 0.38 * w, y0 + 0.45 * h, x0 + 0.92 * w, y0 + 0.88 * h); P.text(x0 + 0.62 * w, y0 + 0.78 * h, name + " out")
    return [x0, y0, x1, y1]


def panels(path):
    c = canvas.Canvas(path, pagesize=letter); out = {}
    def page(key, cells, caption, extra=None):
        P = Page(c); P.prose(60, ["This page holds one figure for the panel tests.", "The caption below names its panels."])
        names = ["North", "South", "East", "West", "Upper", "Lower"]
        out[key] = {"cells": [cell(P, *cc, names[i]) for i, cc in enumerate(cells)]}
        if extra: extra(P)
        y = max(cc[3] for cc in cells) + 24; out[key]["caption"] = P.text(72, y, caption); P.prose(y + 30, ["End of the page text."])
        c.showPage()
    page("cols2", [(70, 160, 290, 320), (320, 160, 540, 320)], "Figure 1: Two stages. (a) The pumping stage. (b) The filtering stage.")
    page("grid4", [(70, 150, 290, 260), (320, 150, 540, 260), (70, 290, 290, 400), (320, 290, 540, 400)],
         "Figure 2: Four sites. (a) North site. (b) South site. (c) East site. (d) West site.")
    page("cols3_cap2", [(70, 160, 220, 320), (250, 160, 390, 320), (420, 160, 540, 320)], "Figure 3: Two parts. (a) The first part. (b) The second part.")
    page("narrow", [(70, 160, 303, 320), (307, 160, 540, 320)], "Figure 4: Two halves. (a) The left half. (b) The right half.")
    page("rows2", [(70, 150, 540, 260), (70, 290, 540, 400)], "Figure 5: Two levels. (a) The upper level. (b) The lower level.")
    page("arrow", [(70, 160, 290, 320), (320, 160, 540, 320)], "Figure 6: Two linked stages. (a) The source stage. (b) The sink stage.",
         extra=lambda P: P.line([(262, 250), (348, 250)], lw=1.2))
    c.save(); GT["panels"] = out


def single(path):
    c = canvas.Canvas(path, pagesize=letter); P = Page(c)
    P.prose(60, ["The storage figure below has one tank and nothing else.", "It is used to test the candidate limit."])
    o = {"tank": P.rect(200, 150, 400, 300, lw=1.5), "valve": P.rect(280, 200, 320, 240)}
    o["caption"] = P.text(72, 330, "Figure 1: The storage tank.")
    P.prose(360, ["The tank holds the treated water until it is pumped out."])
    c.showPage(); c.save(); GT["single"] = o


def area(path):
    """A2.12 area-read page: running header and footer close to the figure block, prose above, a labelled figure, caption, prose
    below. Areas (top-left page points) are recorded in GT for the suite."""
    c = canvas.Canvas(path, pagesize=letter); P = Page(c); o = {}
    o["header"] = P.text(72, 52, "Water Works Journal 12 (2026) 41-58", size=8)
    o["above"] = [P.text(72, 80 + 13 * i, t, size=10) for i, t in enumerate((
        "The second plant was rebuilt in two stages over one summer.",
        "Its new line pushes raw water through four basins in a row.",
        "Each basin has its own pump and its own level gauge."))]
    o["fig_items"] = []
    for i, (name, x) in enumerate((("Inlet basin", 80), ("Mixer", 200), ("Settler", 320), ("Outlet", 440))):
        o["fig_items"].append(P.rect(x, 140, x + 95, 200)); P.text(x + 8, 175, name)
        P.rect(x + 30, 215, x + 65, 240, lw=0.6); P.text(x + 4, 252, name.split()[0] + " gauge", size=7)
    P.line([(175, 170), (200, 170)]); P.line([(295, 170), (320, 170)]); P.line([(415, 170), (440, 170)])
    o["figure"] = [79.5, 139.5, 535.5, 254.0]
    o["caption"] = P.text(72, 272, "Figure 1: Basin line of the second plant after the rebuild in summer.")
    o["below"] = [P.text(72, 292 + 13 * i, t, size=10) for i, t in enumerate((
        "The mixer runs at a slower speed than in the first plant.",
        "Operators report fewer blockages since the rebuild.",
        "A spare pump waits in the store room next to the gate."))]
    o["footer"] = P.text(72, 340, "Page 43 of the water works issue", size=8)
    o["areas"] = {"fig_caption_para": [70, 130, 545, 324], "para_above_and_fig": [66, 66, 545, 258]}
    c.showPage()
    P = Page(c); q = {}
    q["header"] = P.text(72, 52, "Water Works Journal 12 (2026) 41-58", size=8)
    q["above"] = [P.text(72, 80 + 13 * i, t, size=10) for i, t in enumerate((
        "The third plant treats river water with ozone and carbon.",
        "Its control room watches every stage from one large screen.",
        "Night staff can stop each stage with a single switch."))]
    names = (("Raw water tank", 80), ("Ozone reactor", 200), ("Carbon filter", 320), ("Chlorine dosing", 440))
    for name, x in names:
        P.rect(x, 135, x + 100, 195); P.text(x + 6, 168, name)
        P.rect(x + 20, 205, x + 80, 232, lw=0.6); P.text(x + 22, 222, name.split()[0] + " probe", size=8)
    q["figure"] = [79.5, 134.5, 540.5, 232.5]
    q["caption"] = P.text(72, 255, "Figure 2: Ozone and carbon line of the third plant.")
    q["below"] = [P.text(72, 278 + 13 * i, t, size=10) for i, t in enumerate((
        "The reactor needs fresh oxygen every second week.",
        "Staff swap the carbon beds once a year in spring.",
        "Dosing is checked by hand at every shift change."))]
    q["footer"] = P.text(72, 326, "Page 44 of the water works issue", size=8)
    q["areas"] = {"prose_fig_caption": [66, 66, 545, 262], "labels_mostly": [66, 92, 545, 262], "prose_fig_para": [66, 66, 545, 318]}
    c.showPage(); c.save(); o["p2"] = q; GT["area"] = o


def hairline(path):
    """A2.4: a figure made only of text (no graphics the heuristic can pair): caption-only hairline box without the detector."""
    c = canvas.Canvas(path, pagesize=letter); P = Page(c); o = {}
    P.prose(60, ["The schedule figure below is drawn with text only.", "It lists the three shifts of the plant."])
    for i, (a, b) in enumerate((("Early shift", "06:00"), ("Day shift", "14:00"), ("Night shift", "22:00"))):
        P.text(120, 140 + 18 * i, a, size=11); P.text(300, 140 + 18 * i, b, size=11)
    o["caption"] = P.text(72, 220, "Figure 1: Shift schedule of the plant.")
    P.prose(250, ["Each shift hands over to the next at the gate.", "The night shift also runs the cleaning cycle."])
    c.showPage(); c.save(); GT["hairline"] = o


if __name__ == "__main__":
    out = sys.argv[1]; os.makedirs(out, exist_ok=True)
    callouts(os.path.join(out, "callouts.pdf")); panels(os.path.join(out, "panels.pdf")); single(os.path.join(out, "single.pdf"))
    area(os.path.join(out, "area.pdf")); hairline(os.path.join(out, "hairline.pdf"))
    open(os.path.join(out, "notes.md"), "w").write("# Plant notes\n\nThe second plant was rebuilt in two stages over one summer.\n\nIts new line pushes raw water through four basins in a row.\n")
    def rnd(x):
        if isinstance(x, dict): return {k: rnd(v) for k, v in x.items()}
        if isinstance(x, list) and x and isinstance(x[0], (int, float)): return r1(x)
        if isinstance(x, list): return [rnd(v) for v in x]
        return x
    json.dump(rnd(GT), open(os.path.join(out, "p5_gt.json"), "w"), indent=1)
    print(out)
