#!/usr/bin/env python3
"""One scheduler tick: poll feeds -> acquire transcripts -> process briefs -> deliver.

Designed to run every 15 minutes via launchd/Hermes cron. Idempotent: each stage
picks up only unprocessed work; crashes are safe (state machine in SQLite).
"""
import os
import sys

sys.path.insert(0, "/Users/openclaw/yt-research-pipeline/src")
from ytbrief import connect
from ytbrief.poller import poll_once
from ytbrief import transcripts as tr
from ytbrief import brief_worker as bw

MAX_TRANSCRIPTS_PER_TICK = 4
MAX_BRIEFS_PER_TICK = 2  # LLM cost control; queue drains over multiple ticks


def tick() -> dict:
    con = connect()
    out = {}

    # 1. Discovery
    out["poll"] = poll_once(con)

    # 2. Transcripts for discovered videos (oldest-first, capped)
    vids = [r["video_id"] for r in con.execute(
        """SELECT v.video_id FROM videos v
           LEFT JOIN transcripts t ON t.video_id = v.video_id
           WHERE v.state='discovered' AND t.video_id IS NULL
           ORDER BY v.priority DESC, v.published_at ASC LIMIT ?""",
        (MAX_TRANSCRIPTS_PER_TICK,))]
    t_ok = t_miss = 0
    for vid in vids:
        if tr.acquire(con, vid):
            t_ok += 1
        else:
            t_miss += 1
    out["transcripts"] = {"ok": t_ok, "missing": t_miss}

    # 3. Briefs for transcript-ready videos (manual links first via priority)
    bvids = [r["video_id"] for r in con.execute(
        """SELECT v.video_id FROM videos v
           JOIN transcripts t ON t.video_id = v.video_id
           WHERE v.state='transcript_ready'
           ORDER BY v.priority DESC, v.published_at ASC LIMIT ?""",
        (MAX_BRIEFS_PER_TICK,))]
    made = failed = 0
    for vid in bvids:
        try:
            bw.process_video(con, vid)
            made += 1
        except Exception as exc:
            failed += 1
            con.execute("UPDATE videos SET state='dead_letter', state_reason=? WHERE video_id=?",
                        (f"brief_failed: {str(exc)[:250]}", vid))
            con.commit()
    out["briefs"] = {"made": made, "failed": failed}
    con.close()
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(tick()))
