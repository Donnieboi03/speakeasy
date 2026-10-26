"""Per-process state shared by every backend module. Read and assign it as `state.FOCUS` etc., never copy it
with `from speakeasy_state import FOCUS`: a copied name would not see the next run's value."""
import os, sys, threading

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    import docmap
except Exception:            # pragma: no cover - missing module never breaks speech
    docmap = None

STOP = threading.Event()     # set by SIGTERM (stop / hotkey toggle)
SINK = None                  # callable(sentence, tag) while streaming to TTS, else None
FOCUS = None                 # focus.Focus while a run can show the focus box
DOC = None                   # document.DocJob: the source document (PDF / Markdown) read in the background
AREA_FIT = None              # Read area on a PDF: (scale, dx, dy, rms) page -> screen from the OCR lines
