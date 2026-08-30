#!/usr/bin/env python3
"""Monthly ops digest: pipeline stats + actual/estimated spend + model-check reminder.

Runs via Hermes cron on the 1st of each month; posts to Market Video Briefs topic.
Also emits a WARN line if projected spend exceeds the budget ceiling.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/Users/openclaw/yt-research-pipeline/src")
from ytbrief import connect

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
TARGET = "telegram:515737487:4607"
MONTHLY_BUDGET = 5.00  # USD ceiling


def month_bounds(dt: datetime):
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def build_digest() -> str:
    con = connect()
    now = datetime.utcnow()
    start_dt, end_dt = month_bounds(now)
    prev_start_dt, prev_end_dt = month_bounds(start_dt - timedelta(days=1))
    start, end = start_dt.isoformat(), end_dt.isoformat()
    prev_start, prev_end = prev_start_dt.isoformat(), prev_end_dt.isoformat()

    def stats(ws, we, label):
        briefs = con.execute("SELECT COUNT(*) FROM briefs WHERE created_at>=? AND created_at<?", (ws, we)).fetchone()[0]
        cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM briefs WHERE created_at>=? AND created_at<?", (ws, we)).fetchone()[0]
        toks_in = con.execute("SELECT COALESCE(SUM(input_tokens),0) FROM briefs WHERE created_at>=? AND created_at<?", (ws, we)).fetchone()[0]
        unverified = con.execute("""SELECT COUNT(*) FROM claims c JOIN briefs b ON b.brief_id=c.brief_id
                                    WHERE c.label='Unverified' AND b.created_at>=? AND b.created_at<?""", (ws, we)).fetchone()[0]
        claims = con.execute("""SELECT COUNT(*) FROM claims c JOIN briefs b ON b.brief_id=c.brief_id
                                WHERE b.created_at>=? AND b.created_at<?""", (ws, we)).fetchone()[0]
        return {"label": label, "briefs": briefs, "cost": cost, "toks_in": toks_in,
                "claims": claims, "unverified": unverified}

    this = stats(start, end, now.strftime("%B"))
    prev = stats(prev_start, prev_end, (start_dt - timedelta(days=1)).strftime("%B"))
    all_time_cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM briefs").fetchone()[0]
    n_videos = con.execute("SELECT COUNT(*) FROM videos WHERE state IN ('delivered','analyzed')").fetchone()[0]
    dead = con.execute("SELECT COUNT(*) FROM videos WHERE state='dead_letter'").fetchone()[0]

    models = con.execute("""SELECT COALESCE(model_label,'(pre-tracking)') AS m, COUNT(*) AS n,
                            COALESCE(SUM(cost_usd),0) AS c FROM briefs
                            WHERE created_at>=? GROUP BY m ORDER BY n DESC""", (start,)).fetchall()

    lines = [f"📊 Pipeline monthly digest — {this['label']}", ""]
    lines.append(f"• Briefs delivered this month: {this['briefs']} ({prev['briefs']} last month)")
    lines.append(f"• Claims in knowledge base: {this['claims']} ({this['unverified']} unverified)")
    lines.append(f"• Videos processed (all time): {n_videos} · dead letters: {dead}")
    lines.append(f"• Spend this month: ${this['cost']:.2f} (prev: ${prev['cost']:.2f}) · all-time: ${all_time_cost:.2f}")
    if this["briefs"]:
        lines.append(f"• Avg cost per brief: ${this['cost']/this['briefs']:.4f}")
    if models:
        lines.append("• Models used: " + " · ".join(f"{r['m']} ×{r['n']} (${r['c']:.2f})" for r in models))
    lines.append("")
    if this["cost"] > MONTHLY_BUDGET:
        lines.append(f"⚠️ BUDGET: ${this['cost']:.2f} exceeds the ${MONTHLY_BUDGET:.2f} ceiling — review model choice / verification depth.")
    else:
        lines.append(f"✅ Budget: ${this['cost']:.2f} of ${MONTHLY_BUDGET:.2f} ceiling")
    lines.append("")
    lines.append("🔄 Monthly model check: pinned chain is GLM-5.3-flash → DeepSeek-v4-flash-0731 → Grok-4.6. New model worth testing? Say BENCHMARK <provider/model> and the pipeline will duel it against the pinned chain on the golden set.")
    con.close()
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="post to Telegram (default: print)")
    args = ap.parse_args()
    digest = build_digest()
    print(digest)
    if args.send:
        subprocess.run([HERMES_BIN, "send", "--to", TARGET, digest], timeout=120)
        print("\n-- sent to", TARGET)
