#!/usr/bin/env python3
"""Phase-1 holdout fixtures: a letter-size PDF whose figure caption and prose carry sentinel words, plus a capture PNG of
page 1. Usage: make_fixtures.py OUTDIR   (needs reportlab + Pillow + pypdfium2 or pymupdf)."""
import json, os, sys
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from PIL import Image, ImageDraw
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
W, H = letter
CAPTION = "Figure 2: Zorblax quintessa yield across mirevolt trials."
PROSE = ["The plumbrine readings in Figure 2 rise after the vexhollow phase.",
         "Each trial lasted forty minutes under steady lighting.",
         "Participants rested between trials for ten minutes."]
def chart(p):
    im = Image.new("RGB", (400, 300), "white"); d = ImageDraw.Draw(im)
    for i, h in enumerate((120, 200, 80, 160)): d.rectangle([40 + i * 85, 280 - h, 100 + i * 85, 280], fill="steelblue")
    d.line([(30, 280), (390, 280)], fill="black", width=2); d.line([(30, 20), (30, 280)], fill="black", width=2); im.save(p)
img = os.path.join(OUT, "_chart.png"); chart(img)
c = canvas.Canvas(os.path.join(OUT, "sentinel.pdf"), pagesize=letter)
c.setFont("Helvetica-Bold", 16); c.drawString(72, H - 72, "Trial Report")
c.setFont("Helvetica", 11); y = H - 100
for ln in PROSE: c.drawString(72, y, ln); y -= 16
c.drawImage(img, 150, H - 420, 300, 225)
c.setFont("Helvetica", 10); c.drawString(72, H - 440, CAPTION)
c.showPage(); c.save(); os.remove(img)
pdf = os.path.join(OUT, "sentinel.pdf"); png = os.path.join(OUT, "page1.png")
try:
    import pypdfium2 as pdfium
    pdfium.PdfDocument(pdf)[0].render(scale=96 / 72).to_pil().save(png)
except Exception:
    import pymupdf
    pymupdf.open(pdf)[0].get_pixmap(dpi=96).save(png)
json.dump({"pdf": pdf, "png": png, "caption": CAPTION, "prose": PROSE}, open(os.path.join(OUT, "fixtures.json"), "w"))
print(OUT)
