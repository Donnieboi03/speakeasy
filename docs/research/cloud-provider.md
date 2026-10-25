# Optional cloud provider (OpenRouter): common practice

- **OpenAI-compatible client.** OpenRouter exposes `/api/v1/chat/completions` (same body, SSE streaming, `Authorization: Bearer`), plus optional `HTTP-Referer` / `X-Title` attribution headers. We reuse the existing local client; only base URL, headers and model change.
- **BYOK, key in the OS keychain.** Raycast (BYOK AI) and open-wispr keep provider keys out of plain config; on macOS the login Keychain via `security` is the standard headless route. The app writes the item with `security -i` (key on stdin, not argv) and `-T /usr/bin/security` so the backend reads it without a prompt.
- **Off by default, per purpose.** Text rewrite and image grounding are separate switches because they send different data (text vs screenshots). A pill icon and settings label say what leaves the Mac.
- **Local fallback.** Any HTTP/network error, missing key, or no first token within 8 s → local model, logged as `llm_fallback`. Streaming kept: fallback only happens before the first token, so speech never mixes providers mid-sentence.
- **Host safety.** `SPEAKEASY_OR_URL` overrides must be `https` and public (same `public_host` check as other fetches).
- **Grounding.** Local qwen3-vl:4b: median centre error 7.7 pt, p90 224 pt, IoU≥0.5 on 1/20, ~4.5 s/call; geometry handles ~95% of boxes. `tools/ground_eval.py` measures OpenRouter vision models the same way (paid; needs `--yes-paid`). Not run.
