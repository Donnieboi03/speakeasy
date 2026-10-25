"""Which OpenAI-compatible endpoint a request goes to: local Ollama (default) or OpenRouter (opt-in, bring your own key).

SPEAKEASY_LLM=local|openrouter     spoken rewrite (text and image explanations)
SPEAKEASY_GROUND=local|openrouter  figure-part grounding (also auto|vlm|florence|off, all local)
SPEAKEASY_OR_MODEL                 OpenRouter model id (default google/gemini-2.5-flash)
The key lives in the macOS Keychain (service "speakeasy-openrouter", account "openrouter"), never in ~/.speakeasy/config;
SPEAKEASY_OR_KEY in the environment overrides it (CI / testing). Text and images only leave the Mac when a switch is on."""
import os, subprocess, urllib.parse

OR_URL = "https://openrouter.ai/api/v1"
OR_DEFAULT_MODEL = "google/gemini-2.5-flash"
KC_SERVICE, KC_ACCOUNT = "speakeasy-openrouter", "openrouter"
_key_cache = {}

def or_key():
    if "k" in _key_cache: return _key_cache["k"]
    k = os.environ.get("SPEAKEASY_OR_KEY") or None
    if not k and os.path.exists("/usr/bin/security"):
        try:
            r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
                               capture_output=True, text=True, timeout=3)
            k = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            k = None
    _key_cache["k"] = k or None
    return _key_cache["k"]

def or_url():
    """OpenRouter base URL; an override must be https on a public host (never a private / local address)."""
    u = os.environ.get("SPEAKEASY_OR_URL", OR_URL).rstrip("/")
    p = urllib.parse.urlparse(u)
    if p.scheme != "https": return None
    if u != OR_URL:
        import docmap
        if not docmap.public_host(u): return None
    return u

def wants(purpose):
    """True when this purpose ('llm' | 'ground') is switched to OpenRouter."""
    var = "SPEAKEASY_LLM" if purpose == "llm" else "SPEAKEASY_GROUND"
    return os.environ.get(var, "local").lower() == "openrouter"

def cloud(purpose):
    """(base_url, headers, model) for OpenRouter when switched on and usable, else None (= local)."""
    if not wants(purpose): return None
    k, u = or_key(), or_url()
    if not k or not u: return None
    return (u, {"Authorization": "Bearer " + k, "HTTP-Referer": "https://github.com/Donnieboi03/speakeasy", "X-Title": "Speakeasy"},
            os.environ.get("SPEAKEASY_OR_MODEL") or OR_DEFAULT_MODEL)

def status(purpose):
    """For the run log: provider and why (no key, bad URL) - never the key or text."""
    if not wants(purpose): return "local", None
    if not or_key(): return "local", "openrouter-no-key"
    if not or_url(): return "local", "openrouter-bad-url"
    return "openrouter", None
