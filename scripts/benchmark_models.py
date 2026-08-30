#!/usr/bin/env python3
"""Model benchmark for the brief pipeline: run N models on the same golden set,
score outputs, and produce a comparison table. The winner gets pinned."""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/Users/openclaw/yt-research-pipeline/src")
from ytbrief import connect

HOME = os.path.expanduser("~")
GOLDEN = [
    # (video_id, description)
    ("GWafMbZLzT4", "3-min Treasury clip (short/thin)"),
    ("_LqxuuQc5Vw", "out-of-scope test: Jan 2024 BTC video — must be handled"),
    ("fOR1uiPD9w4", "16K-char EU/macro video (medium density)"),
]
# NOTE: _LqxuuQc5Vw was marked out_of_scope — exclude from golden; replace with another
GOLDEN = [g for g in GOLDEN if g[0] != "_LqxuuQc5Vw"]
# pick one more from transcript_ready backlog
con = connect()
extra = con.execute("""SELECT v.video_id FROM videos v JOIN transcripts t USING(video_id)
                       WHERE v.state='transcript_ready' LIMIT 1""").fetchone()
if extra:
    GOLDEN.append((extra["video_id"], "backlog transcript_ready video"))
con.close()

MODELS = [
    {"label": "GLM-5.3-flash (OR)", "provider": "openrouter", "model": "z-ai/glm-5.3-flash"},
    {"label": "DeepSeek-v4-flash-0731 (OR)", "provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731"},
    {"label": "Grok-4.6 (xai-oauth)", "provider": "xai-oauth", "model": "grok-4.6"},
]

REQUIRED_LABELS = {"Claim", "Opinion", "Forecast", "Fact", "Verified", "Corroborated",
                   "Disputed", "Unverified", "ASR-risk"}
BANNED = ["this stock will", "clearly undervalued", "guaranteed", "obvious trade",
          "you should buy", "you should sell"]
FOOTER = "Scenarios are analytical, not personalized investment advice"


def run_one(provider: str, model: str, video_id: str) -> dict:
    """Run a single brief generation with a specific provider/model. Returns metrics."""
    from ytbrief import brief_worker as bw
    con = connect()
    v = con.execute("""SELECT v.*, c.title AS channel_title, t.source AS transcript_source, t.file_path
                       FROM videos v JOIN channels c ON c.channel_id=v.channel_id
                       JOIN transcripts t ON t.video_id=v.video_id
                       WHERE v.video_id=?""", (video_id,)).fetchone()
    if not v:
        con.close()
        return {"error": "no transcript"}
    transcript_text = open(v["file_path"]).read()
    con.close()

    prompt = bw.build_prompt(dict(v), transcript_text)
    t0 = time.time()
    try:
        raw = bw.call_llm(prompt, provider=provider, model=model)
    except Exception as exc:
        return {"error": str(exc)[:200], "wall_s": round(time.time()-t0,1)}
    wall = time.time() - t0
    try:
        brief = bw.parse_json_loose(raw)
    except Exception as exc:
        return {"error": f"JSON: {exc}", "wall_s": wall}

    errs = bw.validate(brief)
    claims = brief.get("claims", [])
    labels_used = {c.get("label") for c in claims}
    label_ok = sum(1 for c in claims if c.get("label") in REQUIRED_LABELS)
    card = brief.get("card", "")
    report = brief.get("report_md", "")
    return {
        "wall_s": round(wall, 1),
        "json_ok": True,
        "validation_errors": errs,
        "n_claims": len(claims),
        "labels_valid": f"{label_ok}/{len(claims)}",
        "pivotality": brief.get("pivotality"),
        "card_chars": len(card),
        "regime_separated": bool(brief.get("regime_speaker") and brief.get("regime_analyst")),
        "footer_present": FOOTER in card or FOOTER in report,
        "has_ts_claims": sum(1 for c in claims if c.get("ts_start")),
        "report_chars": len(report),
    }


def main():
    results = {}
    for m in MODELS:
        results[m["label"]] = {}
        print(f"\n=== {m['label']} ===")
        for vid, desc in GOLDEN:
            print(f"  running {vid} ({desc})...")
            res = run_one(m["provider"], m["model"], vid)
            results[m["label"]][vid] = res
            print(f"    -> {json.dumps(res)[:220]}")

    out = os.path.join(HOME, "yt-research-pipeline/data/benchmark_results.json")
    with open(out, "w") as f:
        json.dump({"golden": GOLDEN, "results": results}, f, indent=2)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
