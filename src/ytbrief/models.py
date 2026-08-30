"""Model pinning + fallback chain for the brief pipeline.

Primary:  GLM-5.3-flash via OpenRouter (pinned 2026-08-30 per benchmark)
Backup 1: DeepSeek-v4-flash-0731 via OpenRouter
Backup 2: Grok-4.6 via xai-oauth (different provider — covers OpenRouter outage)

If the pipeline falls back, a Telegram notification explains why.
"""
import subprocess

NOTIFY_TARGET = "telegram:515737487:4607"  # Market Video Briefs (ops notices ride the same topic for now)

MODEL_CHAIN = [
    {"label": "GLM-5.3-flash", "provider": "openrouter", "model": "z-ai/glm-5.3-flash", "pin": "primary"},
    {"label": "DeepSeek-v4-flash-0731", "provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731", "pin": "backup-1"},
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


def call_llm_with_fallback(prompt: str, video_id: str = "?") -> tuple[str, dict, list[dict]]:
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
        try:
            text = call_llm(prompt, provider=m["provider"], model=m["model"])
            if not text or len(text.strip()) < 50:
                raise RuntimeError("empty/too-short response")
            return text, m, failures
        except Exception as exc:
            failures.append({"label": m["label"], "provider": m["provider"],
                             "model": m["model"], "error": str(exc)[:200]})
    raise RuntimeError(f"All models failed: {failures}")
