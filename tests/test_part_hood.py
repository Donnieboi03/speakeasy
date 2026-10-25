"""A figure part's oval stays inside the label's own panel (Berkerly Fig. 4: the 4a "WANDmini" caption sits just above
panel 4b, and its neighbourhood used to swallow a 4b box, drawing the oval ~100 pt too low)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import docmap

FIG = {"bbox": [41.4, 48.3, 559.5, 387.5],
       "panels": [{"label": "a", "bbox": [42.6, 51.6, 559.5, 233.6]}, {"label": "b", "bbox": [42.5, 233.6, 559.5, 387.5]}],
       "labels": [[420.7, 218.4, 459.7, 224.4, "WANDmini"]],
       "objects": [[430.0, 236.0, 548.7, 299.0, "drawing"],      # a 4b box just below the caption
                   [60.0, 60.0, 140.0, 120.0, "image"]]}        # an unrelated 4a picture

def test_part_stays_in_its_panel():
    r, lab, sc, attached = docmap.ground_label("The WANDmini is secured in an enclosure.", FIG)
    assert lab == "WANDmini" and r[3] <= 233.6 + 0.1, r
    assert not attached and r[1] >= 200, r                       # nothing in 4a touches it: a padded label oval, not a 4b box

if __name__ == "__main__":
    test_part_stays_in_its_panel(); print("ok")
