#!/usr/bin/env python3
"""One scheduler tick: poll feeds -> acquire transcripts -> process briefs -> deliver.

Designed to run every 15 minutes via launchd/Hermes cron. Idempotent: each stage
picks up only unprocessed work; crashes are safe (state machine in SQLite).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from ytbrief import connect
from ytbrief.poller import poll_once
from ytbrief import transcripts as tr
from ytbrief import brief_worker as bw
from ytbrief import intake

MAX_TRANSCRIPTS_PER_TICK = 6
MAX_BRIEFS_PER_TICK = 6  # LLM cost control; ~$0.01/tick at 4 briefs


def tick() -> dict:
    con = connect()
    out = {}

    # 0. Telegram intake (manual links bypass cutoff via origin='manual'; docs registered)
    out["intake"] = {"links": intake.queue_links(con)}

    # 1. Discovery (automatic — cutoff enforced inside poller)
    out["poll"] = poll_once(con)

    # 2. Transcripts for discovered videos (oldest-first, capped)
    # NEWEST first for discovered (drain recent backlog; oldest = stale relevance),
    # retries (waiting_captions) get a separate smaller budget so repeated failures
    # can't starve fresh work (head-of-line blocking bug).
    import time as _time
    now_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    vids = [r["video_id"] for r in con.execute(
        """SELECT v.video_id FROM videos v
           LEFT JOIN transcripts t ON t.video_id = v.video_id
           WHERE v.state='discovered' AND t.video_id IS NULL
           ORDER BY v.priority DESC, v.published_at DESC LIMIT ?""",
        (MAX_TRANSCRIPTS_PER_TICK - 1,))]
    # one retry slot for waiting_captions older than 1h (backoff)
    retry = con.execute(
        """SELECT v.video_id FROM videos v
           LEFT JOIN transcripts t ON t.video_id = v.video_id
           WHERE v.state='waiting_captions' AND t.video_id IS NULL
             AND v.discovered_at < ?
           ORDER BY v.published_at ASC LIMIT 1""",
        (now_iso,)).fetchall()
    vids += [r["video_id"] for r in retry]
    # manual submissions bypass the Aug-25 cutoff: they are never filtered by date
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

    # 4. External documents (user-posted .txt/.md transcripts)
    from ytbrief import external_docs as exd
    docs_ready = [r["doc_id"] for r in con.execute(
        "SELECT doc_id FROM external_docs WHERE state='transcript_ready' LIMIT ?", (MAX_BRIEFS_PER_TICK,))]
    d_made = d_failed = 0
    for doc_id in docs_ready:
        try:
            exd.process_document(con, doc_id)
            d_made += 1
        except Exception as exc:
            d_failed += 1
            con.execute("UPDATE external_docs SET state='dead_letter' WHERE doc_id=?", (doc_id,))
            con.commit()
    out["external_docs"] = {"made": d_made, "failed": d_failed}

    con.close()
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(tick()))
