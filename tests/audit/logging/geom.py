"""Synthetic screen geometry: place a rendered PDF page on a virtual screen (points), crop a capture region at a backing scale."""
import subprocess, json, os, sys
VENV = os.environ.get("DOC_VENV", sys.prefix)
K = 1.594  # screen pt per PDF pt at 100% (measured from Donovan's Cursor viewer: 130% -> 2.07)
def page_to_screen(b, g):
    s = g["zoom"] * K; return [g["ox"] + b[0] * s, g["oy"] + b[1] * s, g["ox"] + b[2] * s, g["oy"] + b[3] * s]
def xywh(b): return [b[0], b[1], b[2] - b[0], b[3] - b[1]]
def render_capture(pdf, g, cap, out):
    """cap = [x,y,w,h] screen pt. Renders the page at zoom*K*backing and crops the capture (white outside the page)."""
    code = f"""
import pymupdf
from PIL import Image
g={json.dumps(g)}; cap={json.dumps(cap)}; r=g['backing']; s=g['zoom']*{K}*r
p=pymupdf.open({pdf!r})[0]; pm=p.get_pixmap(matrix=pymupdf.Matrix(s,s)); pg=Image.frombytes('RGB',(pm.width,pm.height),pm.samples)
im=Image.new('RGB',(int(cap[2]*r),int(cap[3]*r)),(40,40,40))
im.paste(pg,(int(round((g['ox']-cap[0])*r)),int(round((g['oy']-cap[1])*r))))
im.save({out!r})
"""
    subprocess.run([os.path.join(VENV, "bin/python"), "-c", code], check=True)
