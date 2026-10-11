#!/usr/bin/env python3
"""Monthly ops digest — panel-revised structure (2026-09-01).

Structure per expert panel:
  1. PREVIOUS MONTH (complete) — headline detail
  2. Month-to-date for the new month (explicitly partial)
  3. Rolling 30 days (trend view, replaces misleading this-vs-last)
  4. All-time (one line)
  5. Budget vs ceiling
Model-check reminder moved to its own cron (Sept 15).
"""
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ytbrief.config import DELIVERY_TARGET
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from ytbrief import connect

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")

MONTHLY_BUDGET = 5.00  # USD ceiling


def build_digest() -> str:
    con = connect()
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    roll_start = now - timedelta(days=30)

    fmt = lambda dt: dt.strftime("%b %d")
    month_name = prev_start.strftime("%B %Y")

    def stats(ws_dt, we_dt):
        ws, we = ws_dt.isoformat(), we_dt.isoformat()
        briefs = con.execute("SELECT COUNT(*) FROM briefs WHERE created_at>=? AND created_at<?", (ws, we)).fetchone()[0]
        cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM briefs WHERE created_at>=? AND created_at<?", (ws, we)).fetchone()[0]
        claims = con.execute("""SELECT COUNT(*) FROM claims c JOIN briefs b ON b.brief_id=c.brief_id
                                WHERE c.occurred_at>=? AND c.occurred_at<?""", (ws, we)).fetchone()[0]
        unverified = con.execute("""SELECT COUNT(*) FROM claims c JOIN briefs b ON b.brief_id=c.brief_id
                                    WHERE c.label='Unverified' AND c.occurred_at>=? AND c.occurred_at<?""", (ws, we)).fetchone()[0]
        dead = con.execute("""SELECT COUNT(*) FROM videos WHERE state='dead_letter'
                              AND discovered_at>=? AND discovered_at<?""", (ws, we)).fetchone()[0]
        channels = con.execute("""SELECT c.title, COUNT(*) AS n FROM briefs b
                                  JOIN videos v ON v.video_id=b.video_id
                                  JOIN channels c ON c.channel_id=v.channel_id
                                  WHERE b.created_at>=? AND b.created_at<?
                                  GROUP BY c.title ORDER BY n DESC LIMIT 3""", (ws, we)).fetchall()
        return {"briefs": briefs, "cost": cost, "claims": claims, "unverified": unverified,
                "dead": dead,
                "top_channels": ", ".join(f"{r[0]} ({r[1]})" for r in channels) or "—"}

    prev = stats(prev_start, month_start)
    mtd = stats(month_start, now)
    roll = stats(roll_start, now)
    all_briefs = con.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
    all_cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM briefs").fetchone()[0]

    fmt = lambda dt: dt.strftime("%b %d")

    L = [f"📊 Ops Digest — {now.strftime('%b %d, %Y')}", ""]
    # 1. Previous month (complete) — headline
    L.append(f"■ {month_name} (complete)")
    L.append(f"• Briefs delivered: {prev['briefs']}")
    L.append(f"• Spend: ${prev['cost']:.2f}" + (f" (avg ${prev['cost']/prev['briefs']:.4f}/brief)" if prev['briefs'] else ""))
    uv_rate = f"{100*prev['unverified']/prev['claims']:.0f}%" if prev['claims'] else "—"
    shorts = con.execute("""SELECT COUNT(*) FROM videos WHERE state='out_of_scope'
                            AND state_reason LIKE 'Short — excluded%'
                            AND discovered_at>=? AND discovered_at<?""", (prev_start.isoformat(), month_start.isoformat())).fetchone()[0]
    L.append(f"• Unverified rate: {uv_rate} • Dead letters: {prev['dead']} • Shorts excluded: {shorts}")
    L.append(f"• Top channels: {prev['top_channels']}")
    ok = "✅" if prev['cost'] <= MONTHLY_BUDGET else "⚠️"
    L.append(f"• Budget: {ok} ${prev['cost']:.2f} of ${MONTHLY_BUDGET:.2f} ceiling")
    L.append("")
    # 2. Month to date
    L.append(f"■ {now.strftime('%B')} so far (month-to-date, through {now.strftime('%b %d')} {now.strftime('%H:%M')} UTC)")
    L.append(f"• Briefs: {mtd['briefs']} • Spend: ${mtd['cost']:.2f}")
    L.append("")
    # 3. Rolling 30 days
    L.append(f"■ Rolling 30 days ({fmt(roll_start)}–{fmt(now)})")
    avg = f" • Avg: ${roll['cost']/roll['briefs']:.4f}/brief" if roll["briefs"] else ""
    L.append(f"• Briefs: {roll['briefs']} • Spend: ${roll['cost']:.2f}{avg}")
    L.append("")
    # 4. All-time
    L.append(f"■ All-time: {all_briefs} briefs • ${all_cost:.2f} spent")
    con.close()
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="post to Telegram (default: print)")
    args = ap.parse_args()
    digest = build_digest()
    print(digest)
    if args.send:
        subprocess.run(["hermes", "send", "--to", DELIVERY_TARGET, digest], timeout=120)
        print("\n-- sent to", DELIVERY_TARGET)
