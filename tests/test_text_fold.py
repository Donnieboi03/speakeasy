#!/usr/bin/env python3
"""Implementer unit tests for src/textfold.py and src/sentence_map.py (Phase 3 sentence IDs). Run: python3 tests/test_text_fold.py"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import textfold
import sentence_map as SM

def test_shared_vectors():
    for v in json.load(open(os.path.join(HERE, "text_fold_vectors.json"), encoding="utf-8")):
        key, offs = textfold.fold(v["input"])
        assert key == v["key"], (v["input"], key)
        assert textfold.fold_key(v["input"]) == v["key"], v["input"]
        assert textfold.fold_tokens(v["input"]) == v["tokens"], (v["input"], textfold.fold_tokens(v["input"]))
        assert textfold.match_tokens(v["input"]) == v["match_tokens"], (v["input"], textfold.match_tokens(v["input"]))
        assert len(offs) == len(key)

def test_offset_map():
    key, offs = textfold.fold("classi\ufb01cation")
    assert offs[6] == 6 and offs[7] == 6 and offs[8] == 7
    text = "classi-\nfication"; key, offs = textfold.fold(text)
    assert all(text[o] not in "-\n" for o in offs)
    key, offs = textfold.fold("Twenty-five dogs")
    assert key.startswith("25") and offs[:2] == [0, 7]

def test_whole_words_only():
    assert textfold.fold_key("someone") == "someone"
    assert textfold.fold_key("twenty fives") == "20fives"
    assert textfold.fold_key("Seventeen and seventy-one") == "17and71"
    assert textfold.fold_tokens("25% of") == ["25", "percent", "of"]

def test_splitter():
    t = "This is Fig. 5 of the paper. We use e.g. 3.2 units here! A manage-\nment line.\n\nHeading Here\nNext sentence starts. - bullet item\n- second"
    units = SM.split_sentences(t)
    texts = [t[a:b] for a, b in units]
    assert texts[0] == "This is Fig. 5 of the paper.", texts
    assert texts[1] == "We use e.g. 3.2 units here!", texts
    assert texts[2] == "A manage-\nment line.", texts
    assert texts[3] == "Heading Here", texts
    assert texts[-1] == "- second", texts
    assert SM.split_sentences("no end at all\nsecond line") == [(0, 25)]

def bounds(n):
    return [[10.0, 100.0 + 14 * i, 300.0, 12.0] for i in range(n)]

def test_alignment_forward_and_holds():
    lines = ["We study models. The first model is", "small and fast. The second model", "is large. The first model is", "small and fast."]
    text = "\n".join(lines)
    m = SM.SentenceMap(text, bounds(len(lines)))
    a = m.align("The first model is small and fast.")
    assert a.kind == "verbatim" and a.score == 1.0 and a.runner_up == 1.0
    m.commit(a); b = m.band_for(a)
    assert m.sentence_ids(a.key_start, a.key_end) == [1] and [r[1] for r in b["rects"]] == [100.0, 114.0]
    a2 = m.align("The second model is large.")
    assert a2 and a2.kind == "verbatim"; m.commit(a2)
    a3 = m.align("The first model is small and fast.")      # repeated text: its next occurrence
    assert a3 and m.sentence_ids(a3.key_start, a3.key_end) == [3]
    m.commit(a3)
    assert m.align("We study models.") is None
    assert m.align_back("We study models.") is not None

def test_clause_chunks_cover_sentence():
    lines = ["This paper presents a knowledge-based diabetes manage-", "ment system that is implemented through the utilization of", "LLM-powered CHAs. We customize"]
    m = SM.SentenceMap("\n".join(lines), bounds(3))
    a = m.align("This paper presents a knowledge-based")
    m.commit(a); assert [r[1] for r in m.band_for(a)["rects"]] == [100.0]
    b = m.align("diabetes management system that is implemented through the utilization of LLM-powered CHAs.")
    assert b.kind == "verbatim"; m.commit(b)
    assert [r[1] for r in m.band_for(b)["rects"]] == [100.0, 114.0, 128.0]

def test_near_verbatim_and_fuzzy():
    text = "The encoder maps an input sequence of symbol representations to a sequence of continuous representations. Given z, the decoder then generates an output sequence of symbols one element at a time."
    m = SM.SentenceMap(text, None)
    a = m.align("The encoder maps an input sequence of symbol representation to a sequence of continuous representations.")
    assert a.kind == "verbatim" and 0.9 <= a.score < 1.0, a
    m.commit(a)
    f = m.align("Then, given z, the decoder produces the output symbols one at a time.")
    assert f and f.kind == "fuzzy" and f.score >= 0.6, f
    assert m.align("Bananas are yellow and grow in tropical places.") is None

def test_page_alignment_run_in_heading_and_reordered_column():
    page = {"lines": [[10, 0, 40, 9, "Intro text above the section."], [10, 10, 40, 19, "Dataset"], [50, 10, 300, 19, "We used a set of three classic"],
                      [10, 22, 300, 31, "constructions from a large bench\x02"], [320, 0, 600, 9, "mark of reading times. The"],
                      [320, 12, 600, 21, "differential pressure was logged."]],
            "blocks": [{"lines": [4, 5]}, {"lines": [0]}, {"lines": [1]}, {"lines": [2, 3]}]}
    text = "Dataset We used a set of three classic\nconstructions from a large bench-\nmark of reading times. The\ndifferential pressure was logged."
    m = SM.SentenceMap(text, None)
    m.attach_page(page, region=[0, 0, 300, 40])
    assert sorted(m.sentences[0]["page_lines"]) == [1, 2, 3, 4], m.sentences[0]["page_lines"]
    assert sorted(m.sentences[1]["page_lines"]) == [4, 5], m.sentences[1]["page_lines"]

def test_next_page_text_stays_unmapped():
    page = {"lines": [[10, 0, 300, 9, "The model reads the whole page at once."], [10, 12, 300, 21, "Results follow in the next part."],
                      [10, 200, 300, 209, "The method was trained for twelve hours on two GPUs."]],
            "blocks": [{"lines": [0, 1]}, {"lines": [2]}]}
    text = "The model reads the whole page at once.\nResults follow in the next part.\nThe method was trained for ten hours on two GPUs."
    m = SM.SentenceMap(text, None)
    m.attach_page(page, region=[0, 0, 310, 25])
    assert [s["page_lines"] for s in m.sentences] == [[0], [1], []], [s["page_lines"] for s in m.sentences]
    assert m.summary()["mapped"] == 2

def test_find_span_covers_the_sentence_chars():
    text = "Dear Lee,\n\nI hope you are well. The rent is due on \u201cMonday\u201d.\n\nBest,\nAnn"
    m = SM.SentenceMap(text, None)
    s = m.find_span("I hope you are well.")
    assert text[s.start:s.end] == "I hope you are well.", text[s.start:s.end]
    s = m.find_span("The rent is due on \u201cMonday\u201d.", s.end)
    assert text[s.start:s.end] == "The rent is due on \u201cMonday\u201d.", text[s.start:s.end]
    assert m.find_span("Here's the email to Lee.").start is None

def test_build_budget():
    para = ("The proposed approach was evaluated with real-world questions about daily meals in comparison to GPT4. "
            "The evaluation centres on the assessment of potential risk associated with recommended options.\n")
    text = (para * 40)[:6000]
    t0 = time.time(); m = SM.SentenceMap(text, None); ms = (time.time() - t0) * 1000
    assert len(m.sentences) > 40 and ms < 25, ms

if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"ok   {name}")
            except AssertionError as e: fails += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
