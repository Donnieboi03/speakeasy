#!/usr/bin/env python3
"""Synthetic paper page reproducing the Fig. 5 layout: a ruled Table 1 directly above a framed two-panel Figure 5, caption below,
two-column prose, footer. Writes the PDF and a JSON of ground-truth boxes in page points (top-left origin, x0,y0,x1,y1)."""
import json, os, sys
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
W, H = 595.28, 790.87
def tl(x0, y0, x1, y1): return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
def build(path):
    c = canvas.Canvas(path, pagesize=(W, H)); gt = {}
    c.setFont("Helvetica", 9)
    y = 60
    for i in range(10):
        c.drawString(40, H - y, f"Prose line {i+1} about ear EEG electrodes and the drowsiness protocol used here."); y += 12
    gt["prose_line_3"] = tl(40, 60 + 2 * 12 - 9, 40 + c.stringWidth("Prose line 3 about ear EEG electrodes and the drowsiness protocol used here.", "Helvetica", 9), 60 + 2 * 12 + 2)
    # Table 1 at y 270..437 (caption + rules + rows)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 280, "Table 1 | Relevant system, WANDmini, and NMIC specifications")
    c.setLineWidth(0.6); c.line(40, H - 290, 300, H - 290)
    c.setFont("Helvetica", 8); rows = [("Maximum Recording Channels", "64"), ("Recording Channels Used", "11"), ("ADC Resolution", "15 bits"), ("Noise Floor", "70 nV/rtHz"), ("Wireless Data Rate", "2 Mbps"), ("Battery Life", "44 h")]
    yy = 302
    for a, b in rows: c.drawString(42, H - yy, a); c.drawString(230, H - yy, b); yy += 14
    c.line(40, H - (yy - 6), 300, H - (yy - 6))
    gt["table1"] = tl(40, 271, 300, yy - 6)
    # right-column prose next to the table
    c.setFont("Helvetica", 9)
    for i in range(8): c.drawString(310, H - (280 + i * 12), f"Right column sentence {i+1} on classifier agreement.")
    # Figure 5: frame 42.5..558.5 x 466.5..694.3 with panels a) b)
    c.setLineWidth(0.8); fx0, fy0, fx1, fy1 = 42.5, 466.5, 558.5, 694.3
    c.rect(fx0, H - 590.1, fx1 - fx0, 590.1 - fy0); c.rect(fx0, H - fy1, fx1 - fx0, fy1 - 591.6)
    c.setFont("Helvetica-Bold", 10); c.drawString(45, H - 478, "a)"); c.drawString(45, H - 604, "b)")
    c.setFont("Helvetica", 9)
    for j, lab in enumerate(("Recordings", "Post-Processing", "Feature Extraction", "Models")):
        for yb in (472, 597):
            c.rect(70 + j * 120, H - (yb + 100 if yb == 472 else yb + 85), 105, 85 if yb == 597 else 100)
            c.drawString(75 + j * 120, H - (yb + 12), lab)
    gt["fig5"] = tl(fx0, fy0, fx1, fy1); gt["fig5a"] = tl(fx0, fy0, fx1, 590.1); gt["fig5b"] = tl(fx0, 591.6, fx1, fy1)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 712, "Fig. 5 | Drowsiness Detector Training and Validation Diagram.")
    c.setFont("Helvetica", 9); c.drawString(40, H - 724, "a Ear ExG recordings are re-referenced and filtered. b Cross-validation is similar.")
    gt["fig5_caption"] = tl(40, 703, 40 + c.stringWidth("a Ear ExG recordings are re-referenced and filtered. b Cross-validation is similar.", "Helvetica", 9), 726)
    c.setFont("Helvetica", 8); c.drawString(40, H - 768, "Synthetic Communications | (2026) 1:1   6")
    c.showPage(); c.save(); return gt
if __name__ == "__main__":
    out = sys.argv[1]; os.makedirs(out, exist_ok=True); gt = build(os.path.join(out, "fig5.pdf"))
    json.dump({"page_size": [W, H], "page": 1, "boxes": gt}, open(os.path.join(out, "fig5_gt.json"), "w"), indent=1); print(json.dumps(gt))
