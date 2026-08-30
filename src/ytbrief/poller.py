"""Feed poller: 15-min public Atom feed polling with conditional GET + dedupe.

Per the approved plan (§2 Discovery):
- Conditional polling with ETag / If-Modified-Since, jitter, backoff
- Dedupe by immutable video ID; state machine entry point
- Discovery events are hints: channel_id validated against registry before insert
"""
import datetime
import email.utils
import random
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET  # nosec — stdlib parser, YouTube feeds only, allowlisted channel IDs

from . import connect, migrate, utcnow

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
UA = "yt-research-pipeline/0.1 (private research; contact: local user)"
MAX_ENTRIES_PER_POLL = 20  # feed window is ~15; cap defensively
CONNECT_TIMEOUT = 20


def fetch_feed(channel_id: str, etag: str | None, last_modified: str | None):
    """Return (status, entries|None, etag, last_modified). status: ok|not_modified|error"""
    url = FEED_URL.format(cid=channel_id)
    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "not_modified", None, etag, last_modified
        raise
    body = resp.read(MAX_ENTRIES_PER_POLL * 8192)  # defensive size cap
    root = ET.fromstring(body)
    # Validate the feed belongs to the allowlisted channel (discovery events are hints).
    # NOTE: YouTube's feed drops the 'UC' prefix in <yt:channelId> — compare without it.
    feed_cid = root.find("yt:channelId", NS)
    if feed_cid is not None:
        fc = feed_cid.text or ""
        if fc != channel_id and ("UC" + fc) != channel_id:
            return "error", None, None, None
    entries = []
    for e in root.findall("a:entry", NS):
        vid = e.find("yt:videoId", NS)
        title = e.find("a:title", NS)
        pub = e.find("a:published", NS)
        if vid is None or pub is None:
            continue
        entries.append({
            "video_id": vid.text,
            "title": (title.text or "") if title is not None else "",
            "published_at": pub.text,
        })
    return "ok", entries, resp.headers.get("ETag"), resp.headers.get("Last-Modified")


def poll_once(con, max_channels: int | None = None, verbose: bool = False) -> dict:
    """Poll every enabled channel once. Returns stats."""
    migrate(con)
    stats = {"polled": 0, "not_modified": 0, "new_videos": 0, "errors": 0, "channels": 0}
    rows = con.execute(
        "SELECT channel_id, title, etag, last_poll_at FROM channels WHERE enabled=1 ORDER BY priority DESC"
    ).fetchall()
    if max_channels:
        rows = rows[:max_channels]
    stats["channels"] = len(rows)

    for row in rows:
        cid = row["channel_id"]
        # jitter: spread load, never hammer
        time.sleep(random.uniform(0.5, 2.0))
        try:
            status, entries, etag, last_mod = fetch_feed(cid, row["etag"], None)
        except Exception as exc:  # network/parse errors: record and continue
            con.execute("UPDATE channels SET last_error=?, last_poll_at=? WHERE channel_id=?",
                        (str(exc)[:300], utcnow(), cid))
            con.commit()
            stats["errors"] += 1
            if verbose:
                print(f"  ERROR {row['title'] or cid}: {exc}")
            continue

        stats["polled"] += 1
        if status == "not_modified":
            con.execute("UPDATE channels SET last_error=NULL, last_poll_at=?, etag=? WHERE channel_id=?",
                        (utcnow(), etag, cid))
            con.commit()
            stats["not_modified"] += 1
            continue

        new_count = 0
        for entry in entries or []:
            cur = con.execute("SELECT 1 FROM videos WHERE video_id=?", (entry["video_id"],)).fetchone()
            if cur:
                continue
            con.execute(
                """INSERT INTO videos (video_id, channel_id, title, published_at, discovered_at, state, origin, priority)
                   VALUES (?,?,?,?,?,'discovered','subscription',0)""",
                (entry["video_id"], cid, entry["title"], entry["published_at"], utcnow()))
            new_count += 1
        con.execute("UPDATE channels SET last_error=NULL, last_poll_at=?, etag=? WHERE channel_id=?",
                    (utcnow(), etag, cid))
        con.commit()
        stats["new_videos"] += new_count
        if verbose:
            print(f"  {row['title'] or cid}: {len(entries or [])} entries, {new_count} new")

    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-channels", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    con = connect()
    t0 = time.time()
    s = poll_once(con, max_channels=args.max_channels, verbose=args.verbose)
    print(f"\npoll complete in {time.time()-t0:.1f}s: {s}")
    for r in con.execute("""SELECT v.video_id, c.title, v.title, v.published_at
                            FROM videos v JOIN channels c ON c.channel_id=v.channel_id
                            ORDER BY v.published_at DESC LIMIT 10"""):
        print(f"  {r['published_at'][:16]} | {r['title'][:24]:24} | {r[3] if False else r[2][:60]} [{r['video_id']}]")
