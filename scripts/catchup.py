#!/usr/bin/env python3
"""One-shot catch-up: process ALL remaining videos (Aug 30+) in chronological order.

Resumable: state machine in SQLite makes reruns safe. Posts each brief as it
completes. Continues past individual failures (dead-letters them, keeps going).
"""
import os
import sys
import time

sys.path.insert(0, "/Users/openclaw/yt-research-pipeline/src")
from ytbrief import connect
from ytbrief import transcripts as tr
from ytbrief import brief_worker as bw

CUTOFF = "2026-10-08"
PAUSE_BETWEEN = 5  # seconds between LLM calls


def main():
    con = connect()
    # fetch videos needing work, chronological
    rows = con.execute("""SELECT v.video_id, v.published_at, v.title, c.title AS channel_title,
                                 (t.video_id IS NOT NULL) AS has_t
                          FROM videos v
                          JOIN channels c ON c.channel_id = v.channel_id
                          LEFT JOIN transcripts t ON t.video_id = v.video_id
                          WHERE v.published_at >= ? AND v.state IN ('discovered','transcript_ready')
                          ORDER BY v.published_at DESC""", (CUTOFF,)).fetchall()
    print(f"catch-up: {len(rows)} videos to process (chronological)\n", flush=True)
    made = failed = skipped = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        vid = r["video_id"]
        try:
            # transcript if missing
            if not r["has_t"]:
                path = tr.acquire(con, vid)
                if not path:
                    print(f"[{i}/{len(rows)}] SKIP {vid} ({r['title'][:40]}) — no captions available, queued for retry",
                          flush=True)
                    skipped += 1
                    continue
            # brief + deliver
            brief_id = bw.process_video(con, vid)
            made += 1
            print(f"[{i}/{len(rows)}] DONE {vid} ({r['title'][:40]}) — delivered", flush=True)
        except Exception as exc:
            failed += 1
            print(f"[{i}/{len(rows)}] FAIL {vid}: {str(exc)[:120]}", flush=True)
            con.execute("UPDATE videos SET state='dead_letter', state_reason=? WHERE video_id=?",
                        (f"catchup_failed: {str(exc)[:200]}", vid))
            con.commit()
        time.sleep(PAUSE_BETWEEN)
    elapsed = (time.time() - t0) / 60
    print(f"\ncatch-up complete in {elapsed:.0f} min: {made} delivered, {failed} failed, {skipped} no-captions (will retry via ladder)",
          flush=True)


if __name__ == "__main__":
    main()
