import os
"""Model pinning + fallback chain for the brief pipeline.

Primary:  GLM-5.3-flash via OpenRouter (pinned 2026-08-30 per benchmark)
Backup 1: DeepSeek-v4-flash-0731 via OpenRouter
Backup 2: Grok-4.6 via xai-oauth (different provider — covers OpenRouter outage)

If the pipeline falls back, a Telegram notification explains why.
"""
import subprocess

NOTIFY_TARGET = "telegram:515737487:4607"  # Market Video Briefs (ops notices ride the same topic for now)

PRICING = {  # USD per 1M tokens (in, out) — update when models change
    "z-ai/glm-5.3-flash": (0.150, 0.500),
    "deepseek/deepseek-v4.1-flash": (0.150, 0.600),
    "grok-4.6": (0.0, 0.0),  # xai-oauth subscription — zero marginal cost
}

def estimate_cost(model_slug: str, prompt_chars: int, response_chars: int) -> tuple[int, int, float]:
    """Estimate tokens+cost. ~4 chars/token English. Conservative: verification rounds add
    ~3x prompt re-reads; response includes tool turns."""
    pin, pout = PRICING.get(model_slug, (0.10, 0.30))
    in_tokens = int((prompt_chars + response_chars * 2) / 4)   # context re-sent per verify round
    out_tokens = int(response_chars / 4)
    cost = (in_tokens / 1e6) * pin + (out_tokens / 1e6) * pout
    return in_tokens, out_tokens, round(cost, 5)

MODEL_CHAIN = [
    {"label": "GLM-5.3-flash", "provider": "openrouter", "model": "z-ai/glm-5.3-flash", "pin": "primary"},
    {"label": "DeepSeek-v4.1-flash", "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash", "pin": "backup-1"},
    {"label": "Grok-4.6", "provider": "xai-oauth", "model": "grok-4.6", "pin": "backup-2"},
]

# Override via env: YT_MODEL_PROVIDER / YT_MODEL_MODEL forces a specific model (for challenges)


def notify_fallback(used: dict, failed: list[dict], video_id: str):
    """Tell Telegram why a backup model was used."""
    primary = MODEL_CHAIN[0]["label"]
    tried = "; ".join(f"{f['label']} ({f['error'][:80]})" for f in failed)
    msg = (f"⚠️ Brief pipeline fallback — video {video_id}\n"
           f"Primary model {primary} failed. Used backup: {used['label']}.\n"
           f"Failures: {tried}")
    subprocess.run(["/Users/openclaw/.local/bin/hermes", "send", "--to", NOTIFY_TARGET, msg],
                   capture_output=True, text=True, timeout=60)


HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")

def call_llm(prompt: str, provider: str | None = None, model: str | None = None) -> str:
    """Call Hermes chat non-interactively (-Q: final response only)."""
    import subprocess
    args = [HERMES_BIN, "chat", "-q", prompt, "-Q"]
    if provider:
        args += ["--provider", provider]
    if model:
        args += ["--model", model]
    r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-300:]
        raise RuntimeError(f"hermes chat rc={r.returncode}: {tail}")
    return r.stdout


def call_llm_with_fallback(prompt: str, video_id: str = "?", max_attempts_per_model: int = 2) -> tuple[str, dict, list[dict]]:
    """Try the chain in order. Returns (text, used_model, failures)."""
    import os
    from ytbrief.brief_worker import call_llm

    chain = list(MODEL_CHAIN)
    env_prov, env_model = os.environ.get("YT_MODEL_PROVIDER"), os.environ.get("YT_MODEL_MODEL")
    if env_prov and env_model:
        chain.insert(0, {"label": f"{env_model} (override)", "provider": env_prov,
                         "model": env_model, "pin": "override"})

    failures = []
    for m in chain:
        for attempt in range(1, max_attempts_per_model + 1):
            try:
                text = call_llm(prompt, provider=m["provider"], model=m["model"])
                if not text or len(text.strip()) < 50:
                    raise RuntimeError("empty/too-short response")
                return text, m, failures
            except Exception as exc:
                failures.append({"label": m["label"], "provider": m["provider"],
                                 "model": m["model"], "error": str(exc)[:200],
                                 "attempt": attempt})
    raise RuntimeError(f"All models failed: {failures}")
