#!/usr/bin/env python3
"""Phase-2 holdout fixtures (reportlab, our own synthetic content). Every document carries sentinel words (p2lib.SENT) in
prose, captions, table cells and figure part labels so privacy checks can grep logs for them.
Usage: make_p2docs.py OUTDIR  -> writes the PDFs, a capture PNG and fixtures.json (line texts are NOT stored; tests read
geometry from `docmap.py parse-pdf`)."""
import json, os, sys
from reportlab.lib.pagesizes import letter, A4
from reportlab.pdfgen import canvas
from PIL import Image, ImageDraw
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)


def chart(p, color="steelblue"):
    im = Image.new("RGB", (400, 300), "white"); d = ImageDraw.Draw(im)
    for i, h in enumerate((120, 200, 80, 160)): d.rectangle([40 + i * 85, 280 - h, 100 + i * 85, 280], fill=color)
    d.line([(30, 280), (390, 280)], fill="black", width=2); d.line([(30, 20), (30, 280)], fill="black", width=2); im.save(p)


def twocol(path):
    """A4 two-column page: prose in both columns, a ruled Table 2 directly above a framed two-panel Figure 3 in the left
    column region (Fig. 5 layout, sentinel text), and a standalone ruled Table 4 at the bottom of the right column."""
    W, H = A4; c = canvas.Canvas(path, pagesize=A4, invariant=1); c.setFont("Helvetica", 9)
    L = ["The zorblax electrodes were placed behind each ear before the drive.", "Quintessa readings were sampled at two hundred and fifty hertz.",
         "Each mirevolt session lasted forty minutes on the simulator.", "Plumbrine artefacts were removed with an adaptive filter.",
         "Vexhollow scores were averaged over thirty second windows.", "Grelquist labels came from trained observers in the lab."]
    R = ["Dunmarrow drift stayed below two microvolts per hour.", "Skelvane impedance was checked at the start of every run.",
         "Frumplet events were rare and were kept in the dataset.", "Wexmoor baselines were recorded with the eyes open.",
         "The zorblax montage was identical for all participants.", "Quintessa power rose steadily toward the end of a session."]
    for i, s in enumerate(L): c.drawString(40, H - (60 + 12 * i), s)
    for i, s in enumerate(R): c.drawString(310, H - (60 + 12 * i), s)
    # Table 2 directly above Figure 3 (left half)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 160, "Table 2 | Zorblax mirevolt settings")
    c.setLineWidth(0.6); c.line(40, H - 168, 290, H - 168)
    c.setFont("Helvetica", 8); yy = 180
    for a, b in (("Quintessa rate", "250 Hz"), ("Plumbrine gain", "24"), ("Vexhollow window", "30 s"), ("Grelquist count", "12")):
        c.drawString(42, H - yy, a); c.drawString(200, H - yy, b); yy += 13
    c.line(40, H - (yy - 5), 290, H - (yy - 5))
    # Figure 3: frame with panels a) b), parts with sentinel labels
    c.setLineWidth(0.8); fx0, fy0, fx1, fy1 = 42, 250, 290, 470
    c.rect(fx0, H - 358, fx1 - fx0, 358 - fy0); c.rect(fx0, H - fy1, fx1 - fx0, fy1 - 362)
    c.setFont("Helvetica-Bold", 10); c.drawString(45, H - 262, "a)"); c.drawString(45, H - 374, "b)")
    c.setFont("Helvetica", 8)
    for j, lab in enumerate(("Zorblax unit", "Quintessa hub")):
        c.rect(70 + j * 105, H - 345, 90, 70); c.drawString(74 + j * 105, H - 287, lab)
        c.rect(70 + j * 105, H - 460, 90, 70); c.drawString(74 + j * 105, H - 402, ("Mirevolt", "Skelvane")[j])
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 486, "Figure 3 | Plumbrine vexhollow pipeline.")
    c.setFont("Helvetica", 9); c.drawString(40, H - 498, "a Dunmarrow capture. b Grelquist scoring of frumplet events.")
    # right column continues beside the figure
    for i in range(14): c.drawString(310, H - (170 + 12 * i), f"Wexmoor follow-up note {i + 1} on skelvane agreement.")
    # standalone ruled Table 4, right column bottom
    c.setFont("Helvetica-Bold", 9); c.drawString(310, H - 560, "Table 4 | Frumplet outcomes")
    c.line(310, H - 568, 555, H - 568); c.setFont("Helvetica", 8); yy = 580
    for a, b in (("Zorblax hits", "41"), ("Quintessa misses", "7"), ("Mirevolt total", "48")):
        c.drawString(312, H - yy, a); c.drawString(470, H - yy, b); yy += 13
    c.line(310, H - (yy - 5), 555, H - (yy - 5))
    # left column prose under the figure
    c.setFont("Helvetica", 9)
    for i in range(10): c.drawString(40, H - (530 + 12 * i), f"Plumbrine discussion line {i + 1} about grelquist timing.")
    c.setFont("Helvetica", 7); c.drawString(40, H - 800, "Synthetic Holdout | 2026 | 3")
    c.showPage(); c.save()


def multipage(path, img):
    """Letter, three single-column pages: page 1 prose + heading; page 2 a raster Figure 6 with caption; page 3 only a footer."""
    W, H = letter; c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica-Bold", 15); c.drawString(72, H - 72, "Zorblax Field Notes")
    c.setFont("Helvetica", 11)
    for i in range(12): c.drawString(72, H - (110 + 16 * i), f"Quintessa observation {i + 1} recorded near the mirevolt station today.")
    c.setFont("Helvetica", 8); c.drawString(72, 40, "Page 1"); c.showPage()
    c.setFont("Helvetica", 11); c.drawString(72, H - 72, "The plumbrine chart below summarises the vexhollow counts.")
    c.drawImage(img, 150, H - 340, 300, 225)
    c.setFont("Helvetica", 10); c.drawString(72, H - 360, "Figure 6: Grelquist counts per dunmarrow trial.")
    c.setFont("Helvetica", 11); c.drawString(72, H - 400, "Skelvane values were stable across all trials.")
    c.setFont("Helvetica", 8); c.drawString(72, 40, "Page 2"); c.showPage()
    c.setFont("Helvetica", 8); c.drawString(72, 40, "Page 3"); c.showPage(); c.save()


def sentfig(path, png, img):
    """Phase-1-style page: prose + a raster Figure 2 whose caption carries sentinels; plus a capture PNG of the page."""
    W, H = letter; c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica-Bold", 16); c.drawString(72, H - 72, "Trial Report")
    c.setFont("Helvetica", 11); y = H - 100
    for ln in ("The plumbrine readings in Figure 2 rise after the vexhollow phase.", "Each trial lasted forty minutes under steady lighting.", "Participants rested between trials for ten minutes."):
        c.drawString(72, y, ln); y -= 16
    c.drawImage(img, 150, H - 420, 300, 225)
    c.setFont("Helvetica", 10); c.drawString(72, H - 440, "Figure 2: Zorblax quintessa yield across mirevolt trials.")
    c.showPage(); c.save()
    try:
        import pypdfium2 as pdfium
        pdfium.PdfDocument(path)[0].render(scale=96 / 72).to_pil().save(png)
    except Exception:
        import pymupdf
        pymupdf.open(path)[0].get_pixmap(dpi=96).save(png)


def tiny(path):
    """One short line and a small framed Figure 1 with a one-line caption: small enough that `docmap.py dump` prints it whole."""
    W, H = letter; c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica", 11); c.drawString(72, H - 72, "Short zorblax note.")
    c.setLineWidth(1); c.rect(72, H - 300, 200, 180); c.rect(90, H - 280, 60, 40)
    c.setFont("Helvetica", 10); c.drawString(72, H - 318, "Figure 1: Box.")
    c.showPage(); c.save()


def blank(path):
    c = canvas.Canvas(path, pagesize=letter, invariant=1); c.showPage(); c.save()


if __name__ == "__main__":
    img = os.path.join(OUT, "_chart.png"); chart(img)
    twocol(os.path.join(OUT, "twocol.pdf")); multipage(os.path.join(OUT, "multipage.pdf"), img)
    sentfig(os.path.join(OUT, "sentfig.pdf"), os.path.join(OUT, "sentfig_page1.png"), img); blank(os.path.join(OUT, "blank.pdf")); tiny(os.path.join(OUT, "tiny.pdf"))
    open(os.path.join(OUT, "corrupt.pdf"), "wb").write(b"%PDF-1.4\n%zorblax quintessa\n" + os.urandom(64) + b"\n%%EOF\n")
    os.remove(img)
    json.dump({"twocol": "twocol.pdf", "multipage": "multipage.pdf", "sentfig": "sentfig.pdf", "sentfig_png": "sentfig_page1.png", "blank": "blank.pdf", "tiny": "tiny.pdf",
               "corrupt": "corrupt.pdf", "sentfig_caption": "Figure 2: Zorblax quintessa yield across mirevolt trials."}, open(os.path.join(OUT, "fixtures.json"), "w"))
    print(OUT)
