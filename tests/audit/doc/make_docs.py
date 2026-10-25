#!/usr/bin/env python3
"""Generate PDF/MD fixtures for document-aware focus tests (our own synthetic content)."""
import os, sys
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from PIL import Image, ImageDraw
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(OUT, exist_ok=True)
def chart(p, kind, color):
    im = Image.new("RGB", (400, 300), "white"); d = ImageDraw.Draw(im)
    if kind == "bar":
        for i, h in enumerate((120, 200, 80, 160)): d.rectangle([40 + i * 85, 280 - h, 100 + i * 85, 280], fill=color)
    else:
        pts = [(30 + i * 45, 250 - (i * 23) % 200) for i in range(8)]; d.line(pts, fill=color, width=4)
    d.line([(30, 280), (390, 280)], fill="black", width=2); d.line([(30, 20), (30, 280)], fill="black", width=2); im.save(p)
W, H = letter
BODY1 = ["Ear-worn EEG sensors can track drowsiness during long drives.", "We recorded twelve participants over three sessions each.", "Signals were band-pass filtered between 1 and 40 Hz.", "Table 2 summarizes the participant demographics."]
def para(c, y, lines, size=11):
    c.setFont("Helvetica", size)
    for ln in lines: c.drawString(72, y, ln); y -= 16
    return y
def pdf_paper(path):
    a, b, s = os.path.join(OUT, "_a.png"), os.path.join(OUT, "_b.png"), os.path.join(OUT, "_s.png")
    chart(a, "bar", "steelblue"); chart(b, "line", "darkred"); chart(s, "bar", "seagreen")
    c = canvas.Canvas(path, pagesize=letter)
    # page 1: prose + Table 2 (text table, no figure) — false positive bait
    c.setFont("Helvetica-Bold", 16); c.drawString(72, H - 72, "Ear-EEG Drowsiness Detection")
    y = para(c, H - 110, BODY1)
    y -= 10; c.setFont("Helvetica-Bold", 10); c.drawString(72, y, "Table 2: Participant demographics."); y -= 18
    c.setFont("Helvetica", 10)
    for row in (("Group", "Age", "Sessions"), ("Figure skaters", "24", "3"), ("Drivers", "31", "3"), ("Fig. A cohort", "29", "2")):
        for i, cell in enumerate(row): c.drawString(72 + i * 140, y, cell)
        y -= 14
    y = para(c, y - 20, ["As Figure 3 will show, accuracy rises with session length.", "The discussion in Section 5 revisits this point."])
    c.showPage()
    # page 2: Figure 3 single + Figure 4 with panels (a)(b)
    y = para(c, H - 72, ["Results are summarized below."])
    c.drawImage(s, 150, H - 330, 300, 225); c.setFont("Helvetica", 10)
    c.drawString(72, H - 350, "Figure 3: Classification accuracy per session for all twelve participants.")
    c.drawImage(a, 72, H - 610, 220, 165); c.drawImage(b, 320, H - 610, 220, 165)
    c.drawString(170, H - 625, "(a)"); c.drawString(420, H - 625, "(b)")
    c.drawString(72, H - 650, "Figure 4: (a) Alpha power by condition. (b) Drowsiness score over a 60 minute drive.")
    para(c, H - 690, ["The drowsiness score in Figure 4b climbs steadily after minute 30."], 11)
    c.showPage(); c.save()
    for f in (a, b, s): os.remove(f)
def pdf_prose(path):
    c = canvas.Canvas(path, pagesize=letter)
    para(c, H - 72, ["Table 2 summarizes the results, and Figure 7 is discussed in the appendix.", "No graphics appear in this document.", "Fig. 9 of the earlier report was retracted."] * 3)
    c.showPage(); c.save()
MD = """# Ear-EEG Notes

Ear-worn EEG sensors can track drowsiness during long drives.

Table 2 summarizes the participant demographics.

| Group | Age |
|---|---|
| Figure skaters | 24 |
| Drivers | 31 |

![Alpha power by condition](alpha.png)

*Figure 1: Alpha power by condition across all sessions.*

```python
fig = plt.figure()  # Figure 9 is not a real figure
```

- Recorded twelve participants
- Filtered between 1 and 40 Hz
"""
if __name__ == "__main__":
    pdf_paper(os.path.join(OUT, "paper.pdf")); pdf_prose(os.path.join(OUT, "prose.pdf"))
    open(os.path.join(OUT, "notes.md"), "w").write(MD); chart(os.path.join(OUT, "alpha.png"), "bar", "steelblue")
    print(OUT)
