"""Telegram intake: watch for YouTube links + transcript documents posted by the user.

Two paths:
1. Links in Market Video Briefs topic → normalized to video_id → queued in videos
   table (origin='manual', priority high, cutoff bypassed) → next tick processes.
2. Documents (.txt/.md/.srt/.vtt) → registered in external_docs → next tick briefs.

Runs as a Hermes cron every few minutes; uses `hermes` session store? No —
simplest reliable mechanism: Hermes gateway saves Telegram messages as sessions;
instead we use a Hermes cron job whose prompt instructs the agent to check the
last N messages. But deterministic extraction is better: this script scans the
gateway log for recent inbound messages from the user containing YouTube links
or saved document paths.
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from ytbrief import connect, utcnow

LOG_PATH = os.path.expanduser("~/.hermes/logs/gateway.log")
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)([\w-]{11})")
DOC_HINT_RE = re.compile(r"document|\.txt|\.md|\.srt|\.vtt", re.I)
USER_NAME = "Rick W"
LOOKBACK_MINUTES = 20  # overlap tick cadence; dedupe makes re-processing safe
CUTOFF = "2026-08-25"  # applies to automatic discovery only — manual bypasses it


def scan_recent_messages() -> list[dict]:
    """Extract recent user messages with YouTube links or doc mentions from gateway log."""
    events = []
    if not os.path.exists(LOG_PATH):
        return events
    cutoff_ts = datetime.utcnow() - timedelta(minutes=LOOKBACK_MINUTES)
    with open(LOG_PATH, errors="ignore") as f:
        for line in f:
            if "inbound message" not in line or USER_NAME not in line:
                continue
            m = re.search(r"^(\S+ \S+) .*?msg='(.*?)' reply_to", line)
            if not m:
                continue
            ts_str, msg = m.group(1), m.group(2)
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                continue
            if ts < cutoff_ts:
                continue
            events.append({"ts": ts, "msg": msg})
    return events


def queue_links(con, dry_run=False):
    """Find YouTube links in recent messages, queue as manual high-priority jobs."""
    added = []
    seen = set()
    for ev in scan_recent_messages():
        for vid in YT_RE.findall(ev["msg"]):
            if vid in seen:
                continue
            seen.add(vid)
            if con.execute("SELECT 1 FROM videos WHERE video_id=?", (vid,)).fetchone():
                continue  # already known (dedupe — manual re-link returns existing)
            meta = fetch_metadata(vid)
            if not meta:
                continue
            con.execute("""INSERT INTO videos (video_id, channel_id, title, published_at,
                           discovered_at, state, origin, priority)
                           VALUES (?,?,?,?,?,'discovered','manual',10)""",
                        (vid, meta["channel_id"], meta["title"], meta["published_at"], utcnow()))
            con.commit()
            added.append({"video_id": vid, "title": meta["title"], "channel": meta["channel_title"]})
    return added


def fetch_metadata(vid: str) -> dict | None:
    """Resolve channel + title + publish date via oEmbed + yt-dlp (no login)."""
    import subprocess, json as J
    try:
        r = subprocess.run([YTDLP, "--dump-json", "--no-download",
                            f"https://youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        meta = J.loads(r.stdout.splitlines()[0])
        cid = meta.get("channel_id")
        if not cid:
            return None
        con = connect()
        row = con.execute("SELECT channel_id FROM channels WHERE channel_id=?", (cid,)).fetchone()
        if not row:
            # register channel on demand (manual submissions may be from unsubscribed channels)
            con.execute("""INSERT INTO channels (channel_id, title, enabled, priority, source)
                           VALUES (?,?,0,0,'manual-submission')""",
                        (cid, meta.get("channel") or vid))
            con.commit()
        con.close()
        return {"channel_id": cid, "channel_title": meta.get("channel"),
                "title": meta.get("title") or vid,
                "published_at": meta.get("upload_date") and
                    f"{meta['upload_date'][:4]}-{meta['upload_date'][4:6]}-{meta['upload_date'][6:]}T00:00:00Z"}
    except Exception:
        return None


def handle_documents(con, saved_paths: list[str], dry_run=False):
    """Register documents Hermes saved from Telegram (paths come from the agent relay)."""
    from ytbrief.external_docs import register_document
    out = []
    for p in saved_paths:
        try:
            doc_id = register_document(con, p)
            out.append({"doc_id": doc_id, "path": p})
        except Exception as exc:
            out.append({"path": p, "error": str(exc)[:150]})
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--docs", nargs="*", default=[], help="explicit doc paths to register")
    args = ap.parse_args()
    con = connect()
    links = queue_links(con, dry_run=args.dry_run)
    docs = handle_documents(con, args.docs, dry_run=args.dry_run)
    con.close()
    import json
    print(json.dumps({"links_queued": links, "docs_registered": docs}))
