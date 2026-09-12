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
# Fresh golden set: 3 recent delivered videos spanning thin/medium/dense, deterministic pick
con = connect()
rows = con.execute("""SELECT v.video_id, t.char_count FROM videos v
                      JOIN transcripts t ON t.video_id=v.video_id
                      WHERE v.state='delivered' ORDER BY v.published_at DESC LIMIT 12""").fetchall()
con.close()
if rows:
    rows_sorted = sorted(rows, key=lambda r: r["char_count"])
    thin = rows_sorted[len(rows_sorted)//10]      # ~10th percentile
    dense = rows_sorted[-1]                        # largest
    med = min(rows_sorted, key=lambda r: abs(r["char_count"] - 15000))  # closest to 15K
    GOLDEN = [(thin["video_id"], f"thin ({thin['char_count']} chars)"),
              (med["video_id"], f"medium ({med['char_count']} chars)"),
              (dense["video_id"], f"dense ({dense['char_count']} chars)")]
else:
    GOLDEN = []

MODELS = [
    {"label": "GLM-5.3-flash (OR)", "provider": "openrouter", "model": "z-ai/glm-5.3-flash"},
    {"label": "DeepSeek-v4.1-flash (OR)", "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"},
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
    raw = None
    last_err = None
    for attempt in (1, 2):   # retry once on transient failure
        try:
            raw = bw.call_llm(prompt, provider=provider, model=model)
            break
        except Exception as exc:
            last_err = str(exc)
            last_err_stderr = getattr(exc, "stderr", "") or ""
    wall = time.time() - t0
    if raw is None:
        return {"error": (last_err or "?")[-300:], "wall_s": round(wall,1)}
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

    out = "/Users/openclaw/yt-research-pipeline/data/benchmark_results.json"
    with open(out, "w") as f:
        json.dump({"golden": GOLDEN, "results": results}, f, indent=2)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
