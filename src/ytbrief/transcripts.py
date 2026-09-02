"""Transcript ladder: creator captions -> auto captions (youtube-transcript-api) -> local ASR fallback.

Per plan §4: layered cascade, retries for delayed captions, audio deleted after ASR,
failures categorized (not all treated alike).
"""
import hashlib
import os
import re

from . import connect, utcnow

TRANSCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "transcripts")

# Failure categories (plan §4) — terminal vs retryable
CATEGORIES = {
    "captions_processing": "retryable",     # auto captions may lag upload
    "captions_disabled":   "terminal",
    "video_unavailable":   "terminal",      # private/deleted/members-only
    "throttled":           "retryable",
    "parser_broken":       "retryable",     # library breakage — flag for update
    "asr_failed":          "retryable",
    "unknown":             "retryable",
}


def categorize_error(exc: Exception) -> str:
    from youtube_transcript_api._errors import (
        NoTranscriptFound, TranscriptsDisabled, VideoUnavailable,
        RequestBlocked, IpBlocked,
    )
    name = type(exc).__name__
    if isinstance(exc, TranscriptsDisabled) or name == "TranscriptsDisabled":
        return "captions_disabled"
    if isinstance(exc, VideoUnavailable) or name == "VideoUnavailable":
        return "video_unavailable"
    if isinstance(exc, (RequestBlocked, IpBlocked)) or name in ("RequestBlocked", "IpBlocked"):
        return "throttled"
    if isinstance(exc, NoTranscriptFound) or name == "NoTranscriptFound":
        return "captions_processing"  # may exist later (auto-captions lag)
    return "unknown"


def save_transcript(con, video_id: str, source: str, segments: list[dict],
                    language: str = "en", asr_model: str | None = None) -> str:
    """Persist transcript segments to disk + DB. Returns file path."""
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    lines = []
    for seg in segments:
        start = seg.get("start", 0.0)
        text = (seg.get("text") or "").replace("\n", " ").strip()
        if text:
            mm, ss = divmod(int(start), 60)
            lines.append(f"[{mm:02d}:{ss:02d}] {text}")
    content = "\n".join(lines)
    h = hashlib.sha256(content.encode()).hexdigest()
    path = os.path.join(TRANSCRIPT_DIR, f"{video_id}.txt")
    with open(path, "w") as f:
        f.write(content)
    con.execute(
        """INSERT INTO transcripts (video_id, source, language, char_count, content_hash, file_path, asr_model)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET source=excluded.source, language=excluded.language,
             char_count=excluded.char_count, content_hash=excluded.content_hash,
             file_path=excluded.file_path, asr_model=excluded.asr_model""",
        (video_id, source, language, len(content), h, path, asr_model))
    con.execute("UPDATE videos SET state='transcript_ready' WHERE video_id=?", (video_id,))
    con.commit()
    return path


def is_short_canonical(video_id: str) -> bool | None:
    """YouTube's own classifier: /shorts/<id> stays on /shorts/ iff it's a Short.
    Returns True/False, or None if the check failed (network etc.)."""
    import urllib.request, urllib.error
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return f"/shorts/{video_id}" in resp.geturl()
    except Exception:
        return None


def fetch_duration(con, video_id: str):
    """Fetch real duration + description + short-classification via yt-dlp (no download)."""
    import subprocess, json as _json
    try:
        r = subprocess.run(["/Users/openclaw/yt-research-pipeline/.venv/bin/yt-dlp", "--dump-json", "--no-download", f"https://youtube.com/watch?v={video_id}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            meta = _json.loads(r.stdout.splitlines()[0])
            dur = int(meta.get("duration") or 0)
            desc = (meta.get("description") or "")[:4000]
            w, h = meta.get("width"), meta.get("height")
            portrait = bool(w and h and h > w)
            # canonical YouTube classification corroborated by aspect+duration
            canonical = is_short_canonical(video_id)
            if canonical is True:
                is_short = 1
            elif canonical is False and portrait and dur and dur <= 180:
                is_short = 1   # vertical <=3min: Short by any reasonable definition
            elif canonical is False:
                is_short = 0
            else:
                is_short = 1 if (portrait and dur and dur <= 180) else None
            con.execute("UPDATE videos SET duration_s=?, description=?, is_short=? WHERE video_id=?",
                        (dur, desc, is_short, video_id))
            con.commit()
            return dur
    except Exception:
        pass
    return None


def try_transcript_api(con, video_id: str) -> str | None:
    """Rung 1+2: creator captions or auto captions via youtube-transcript-api."""
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        segments = [{"start": s.start, "text": s.text} for s in fetched]
        # distinguish source: manual vs generated
        is_generated = getattr(fetched, "is_generated", True)
        source = "auto_caption" if is_generated else "creator"
        return save_transcript(con, video_id, source, segments)
    except Exception as exc:
        cat = categorize_error(exc)
        _record_failure(con, video_id, cat, str(exc)[:300])
        return None


def _record_failure(con, video_id: str, category: str, detail: str):
    retryable = CATEGORIES.get(category, "retryable") == "retryable"
    con.execute(
        """UPDATE videos SET state=?, state_reason=? WHERE video_id=?""",
        ("waiting_captions" if retryable else "dead_letter",
         f"{category}: {detail}", video_id))
    con.commit()


def asr_fallback(con, video_id: str) -> str | None:
    """Rung 3: yt-dlp audio download -> local whisper.cpp -> delete audio.
    NOT implemented until whisper.cpp benchmark (plan: benchmark vs MLX first)."""
    # Placeholder: queue for ASR lane rather than fake success.
    con.execute("UPDATE videos SET state='waiting_captions', state_reason='queued_for_asr' WHERE video_id=?",
                (video_id,))
    con.commit()
    return None


def acquire(con, video_id: str, allow_asr: bool = False) -> str | None:
    """Full ladder for one video. Returns transcript path or None."""
    row = con.execute("SELECT origin, is_short FROM videos WHERE video_id=?", (video_id,)).fetchone()
    if row and row["is_short"] is None:
        fetch_duration(con, video_id)
        row = con.execute("SELECT origin, is_short FROM videos WHERE video_id=?", (video_id,)).fetchone()
    # Short exclusion (user directive): canonical + corroborated classification.
    # Manual links bypass — an explicit request is its own authorization.
    if row and row["is_short"] == 1 and row["origin"] != "manual":
        con.execute("UPDATE videos SET state='out_of_scope', state_reason='Short — excluded (user directive)' WHERE video_id=?",
                    (video_id,))
        con.commit()
        return None
    path = try_transcript_api(con, video_id)
    if path:
        return path
    if allow_asr:
        return asr_fallback(con, video_id)
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--video-id", default=None)
    args = ap.parse_args()
    con = connect()
    if args.video_id:
        vids = [args.video_id]
    else:
        vids = [r["video_id"] for r in con.execute(
            """SELECT v.video_id FROM videos v LEFT JOIN transcripts t ON t.video_id=v.video_id
               WHERE t.video_id IS NULL AND v.state='discovered'
               ORDER BY v.published_at DESC LIMIT ?""", (args.limit,))]
    ok = fail = 0
    for vid in vids:
        r = acquire(con, vid)
        if r:
            ok += 1
            row = con.execute("SELECT source, char_count FROM transcripts WHERE video_id=?", (vid,)).fetchone()
            print(f"  OK   {vid}  source={row['source']}  chars={row['char_count']}")
        else:
            fail += 1
            row = con.execute("SELECT state_reason FROM videos WHERE video_id=?", (vid,)).fetchone()
            print(f"  MISS {vid}  {row['state_reason'][:90] if row and row['state_reason'] else '?'}")
    print(f"\n{ok} transcripts, {fail} without captions")
