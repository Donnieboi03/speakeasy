"""Picker provider order and scope checks (phase5-parts.md A2.6, A2.9) with a stubbed transport and render: local first,
OpenRouter only after a local unavailable / no-logprobs with SPEAKEASY_GROUND=openrouter, out-of-scope answers rejected."""
import math, os, sys, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import grounding, provider

CANDS = [[10, 10, 50, 50], [60, 10, 100, 50], [210, 10, 250, 50]]
SCOPE = [0, 0, 120, 100]                                   # panel a; the third candidate sits in panel b

def ollama_reply(letter, p=0.99, other="Z"):
    top = [{"token": letter, "logprob": math.log(p)}, {"token": other, "logprob": math.log(1 - p)}]
    return {"message": {"content": letter}, "logprobs": [{"token": letter, "logprob": math.log(p), "top_logprobs": top}]}

def or_reply(letter, logprobs=False):
    c = {"message": {"content": letter}}
    if logprobs: c["logprobs"] = {"content": [{"token": letter, "logprob": -0.01, "top_logprobs": [{"token": letter, "logprob": -0.01}]}]}
    return {"choices": [c]}

class Stub:
    """Records every request; local answers come from `local`, OpenRouter answers from `cloud` (a value or an exception)."""
    def __init__(self, local, cloud=None):
        self.local, self.cloud, self.calls = local, cloud, []
    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body))
        ans = self.local if url.endswith("/api/chat") else self.cloud
        if isinstance(ans, Exception): raise ans
        return ans

def grounder(stub, mode):
    os.environ["SPEAKEASY_GROUND"] = mode
    g = grounding.Grounder(); g.transport = stub
    g.render = lambda fut, png: ("red", "UE5H")
    return g

def pick(g, scope=SCOPE, region=None):
    f = grounding.PickFuture(1, scope, CANDS, "the pump", {"sent": 0}, region)
    g._pick(f)
    return f

def with_cloud(fn):
    saved = provider.cloud
    provider.cloud = lambda purpose: ("https://openrouter.ai/api/v1", {"Authorization": "Bearer k"}, "google/gemini-x")
    try: fn()
    finally: provider.cloud = saved; os.environ.pop("SPEAKEASY_GROUND", None)

def down():
    return urllib.error.URLError(ConnectionRefusedError(61, "refused"))

def test_local_unavailable_sends_this_and_every_later_pick_to_openrouter():
    def run():
        stub = Stub(down(), or_reply("B", logprobs=True)); g = grounder(stub, "openrouter")
        f1 = pick(g)
        assert f1.outcome == "accepted" and f1.provider == "openrouter" and f1.fallback_from == "unavailable" and f1.confidence == "logprob", vars(f1)
        f2 = pick(g)
        assert f2.provider == "openrouter" and f2.fallback_from is None
        assert [u.endswith("/api/chat") for u, _ in stub.calls] == [True, False, False]    # local once, then OpenRouter only
        assert g.fallback == {"unavailable": 1, "no-logprobs": 0} and g.breaker["local"] == "unavailable"
        body = stub.calls[1][1]
        images = [p for p in body["messages"][0]["content"] if p["type"] == "image_url"]
        texts = [p["text"] for p in body["messages"][0]["content"] if p["type"] == "text"]
        assert len(body["messages"]) == 1 and len(images) == 1 and len(texts) == 1 and texts[0].startswith("This image is part of a figure.")
        assert body["max_tokens"] == 1 and body["logprobs"] is True and body["top_logprobs"] == 20 and body["temperature"] == 0
    with_cloud(run)

def test_local_no_logprobs_falls_back():
    def run():
        stub = Stub({"message": {"content": "A"}}, or_reply("A")); g = grounder(stub, "openrouter")
        f = pick(g)
        assert f.provider == "openrouter" and f.fallback_from == "no-logprobs" and f.outcome == "accepted", vars(f)
        assert f.confidence == "letter-only" and f.decision["p_top"] is None                 # OpenRouter answered without logprobs
    with_cloud(run)

def test_local_low_confidence_never_falls_back():
    def run():
        stub = Stub(ollama_reply("A", 0.6, other="B"), or_reply("B")); g = grounder(stub, "openrouter")
        f = pick(g)
        assert f.outcome == "low-confidence" and f.provider == "local" and len(stub.calls) == 1
    with_cloud(run)

def test_auto_with_local_down_never_calls_openrouter():
    def run():
        stub = Stub(down(), or_reply("B")); g = grounder(stub, "auto")
        f1, f2 = pick(g), pick(g)
        assert f1.outcome == f2.outcome == "unavailable" and f1.provider == f2.provider == "local" and not f2.called
        assert all(u.endswith("/api/chat") for u, _ in stub.calls) and len(stub.calls) == 1
    with_cloud(run)

def test_openrouter_failure_is_unavailable_from_openrouter():
    def run():
        stub = Stub(down(), urllib.error.HTTPError("u", 502, "bad", {}, None)); g = grounder(stub, "openrouter")
        f1 = pick(g); f2 = pick(g)
        assert f1.outcome == "unavailable" and f1.provider == "openrouter" and g.breaker["openrouter"] == "unavailable", vars(f1)
        assert f2.outcome == "unavailable" and f2.provider == "openrouter" and len(stub.calls) == 2   # the OpenRouter breaker holds
    with_cloud(run)

def test_off_menu_letter_only_reply():
    def run():
        g = grounder(Stub(down(), or_reply("Sure")), "openrouter")
        assert pick(g).outcome == "off-menu"
    with_cloud(run)

def test_answer_in_a_sibling_panel_is_out_of_scope():
    g = grounder(Stub(ollama_reply("C")), "local")
    f = pick(g)
    assert f.outcome == "out-of-scope" and f.best() is None, vars(f)
    assert pick(grounder(Stub(ollama_reply("A")), "local")).outcome == "accepted"

def test_answer_outside_the_label_region_is_out_of_scope():
    g = grounder(Stub(ollama_reply("B")), "local")                 # inside the scope, outside the label's clip
    assert pick(g, region=[0, 0, 55, 60]).outcome == "out-of-scope"

def test_guessed_panel_scope_counts_too():
    guessed_a = [0, 0, 55, 100]                                    # an even caption split: panel a ends at x = 55
    g = grounder(Stub(ollama_reply("B")), "local")
    assert pick(g, scope=guessed_a).outcome == "out-of-scope"

if __name__ == "__main__":
    for k, fn in list(globals().items()):
        if k.startswith("test_"): fn()
    print("ok")
