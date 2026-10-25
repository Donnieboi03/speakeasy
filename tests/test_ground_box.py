"""Grounding box conventions: Gemini (via OpenRouter) answers [ymin,xmin,ymax,xmax] 0-1000; Qwen answers [x1,y1,x2,y2]."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import speakeasy as se

def test_gemini_is_y_first():
    assert se.parse_ground_box('{"box_2d": [100, 200, 300, 600]}', "google/gemini-3.5-flash-lite") == [0.2, 0.1, 0.6, 0.3]
    assert "ymin" in se.ground_prompt("google/gemini-3.5-flash-lite")

def test_qwen_is_x_first():
    assert se.parse_ground_box('{"bbox_2d":[100,200,300,600]}', "qwen3-vl:4b-instruct") == [0.1, 0.2, 0.3, 0.6]

def test_unit_scale_and_bad_boxes():
    assert se.parse_ground_box("[0.1,0.2,0.3,0.4]", "qwen3-vl") == [0.1, 0.2, 0.3, 0.4]
    assert se.parse_ground_box("[300,200,100,600]", "qwen3-vl") is None
    assert se.parse_ground_box("no box", "google/gemini-3.5-flash-lite") is None

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
