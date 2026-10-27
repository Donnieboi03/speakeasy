#!/usr/bin/env python3
"""Phase-3 holdout fixtures (reportlab + DejaVu, own synthetic content with p2lib sentinel words).
Usage: make_p3docs.py OUTDIR FONT.ttf -> p3prose.pdf (A4, two columns, 2 pages) and p3sent.json (the line texts as drawn,
the sentences with the indexes of the drawn lines they touch). Geometry is taken from `docmap.py parse-pdf` by the tests."""
import json, os, sys
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

OUT, FONT = sys.argv[1], sys.argv[2]; os.makedirs(OUT, exist_ok=True)
pdfmetrics.registerFont(TTFont("DV", FONT))

# page 1 left column, page 1 right column, page 2 left column (drawn in that order; reading order left -> right -> page 2)
LEFT = ["The zorblax electrodes were placed behind each ear be-",
        "fore the long drive began. Quintessa readings were",
        "sampled at a fixed rate on every channel of the ampli-",
        "fier. Each mirevolt session lasted twenty-five minutes",
        "on the simulator. The classi\ufb01cation step ran of\ufb02ine.",
        "Plumbrine artefacts were removed with an adaptive",
        "filter. The baseline was recorded twice.",
        "Vexhollow scores were averaged over short windows.",
        "The baseline was recorded twice. Grelquist labels",
        "came from trained observers in the lab. Dunmarrow drift",
        "stayed low for the whole session and"]
RIGHT = ["never exceeded two microvolts per hour. Skelvane",
         "impedance was checked at the start of every run.",
         "Frumplet events were rare and were kept in the data-",
         "set. Wexmoor baselines were recorded with the eyes",
         "open. Quintessa gain was fixed at a moderate level",
         "for every recording in the protocol. Mirevolt noise",
         "remained small during the calm segments of driving.",
         "Plumbrine filters were tuned once and never again.",
         "Vexhollow events were counted by two raters.",
         "Grelquist agreement between raters was high.",
         "The zorblax montage was identical for all of the"]
P2 = ["participants in the study. Quintessa power rose",
      "steadily toward the end of a session."]
# sentence -> (text as spoken verbatim, indexes into LINES = LEFT + RIGHT + P2)
SENTS = [("The zorblax electrodes were placed behind each ear before the long drive began.", [0, 1]),
         ("Quintessa readings were sampled at a fixed rate on every channel of the amplifier.", [1, 2, 3]),
         ("Each mirevolt session lasted twenty-five minutes on the simulator.", [3, 4]),
         ("The classification step ran offline.", [4]),
         ("Plumbrine artefacts were removed with an adaptive filter.", [5, 6]),
         ("The baseline was recorded twice.", [6]),
         ("Vexhollow scores were averaged over short windows.", [7]),
         ("The baseline was recorded twice.", [8]),
         ("Grelquist labels came from trained observers in the lab.", [8, 9]),
         ("Dunmarrow drift stayed low for the whole session and never exceeded two microvolts per hour.", [9, 10, 11]),
         ("Skelvane impedance was checked at the start of every run.", [11, 12]),
         ("Frumplet events were rare and were kept in the dataset.", [13, 14]),
         ("Wexmoor baselines were recorded with the eyes open.", [14, 15]),
         ("Quintessa gain was fixed at a moderate level for every recording in the protocol.", [15, 16]),
         ("Mirevolt noise remained small during the calm segments of driving.", [16, 17]),
         ("Plumbrine filters were tuned once and never again.", [18]),
         ("Vexhollow events were counted by two raters.", [19]),
         ("Grelquist agreement between raters was high.", [20]),
         ("The zorblax montage was identical for all of the participants in the study.", [21, 22]),
         ("Quintessa power rose steadily toward the end of a session.", [22, 23])]


def build(path):
    W, H = A4; c = canvas.Canvas(path, pagesize=A4, invariant=1); c.setFont("DV", 9)
    for i, s in enumerate(LEFT): c.drawString(40, H - (60 + 12 * i), s)
    for i, s in enumerate(RIGHT): c.drawString(310, H - (60 + 12 * i), s)
    c.setFont("DV", 7); c.drawString(40, H - 800, "Synthetic Holdout P3 | 1"); c.showPage()
    c.setFont("DV", 9)
    for i, s in enumerate(P2): c.drawString(40, H - (60 + 12 * i), s)
    c.setFont("DV", 7); c.drawString(40, H - 800, "Synthetic Holdout P3 | 2"); c.showPage(); c.save()


if __name__ == "__main__":
    build(os.path.join(OUT, "p3prose.pdf"))
    json.dump({"left": LEFT, "right": RIGHT, "p2": P2, "sents": SENTS}, open(os.path.join(OUT, "p3sent.json"), "w"), indent=1)
    print(OUT)
