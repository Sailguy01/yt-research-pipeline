"""External document intake: .txt/.md video transcripts not on YouTube.

Flow: user posts a document to Telegram → Hermes saves it → this module
registers it, hashes for dedupe, runs the standard brief pipeline.
"""
import hashlib
import os
import shutil

from . import connect, utcnow
from .models import MODEL_CHAIN  # noqa — pins documented here for operators

EXT_DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "external_docs")
ALLOWED_EXT = {".txt", ".md", ".markdown", ".vtt", ".srt"}
ANALYSIS_VERSION = "v2.0-skill"


def register_document(con, src_path: str, title: str | None = None, source_desc: str | None = None) -> str:
    """Register an external transcript. Returns doc_id. Dedupes by content hash."""
    src_path = os.path.abspath(os.path.expanduser(src_path))
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"unsupported file type {ext} (allowed: {sorted(ALLOWED_EXT)})")
    content = open(src_path, encoding="utf-8", errors="replace").read()
    if len(content.strip()) < 200:
        raise ValueError("document too short to be a transcript (<200 chars)")
    h = hashlib.sha256(content.encode()).hexdigest()

    dup = con.execute("SELECT doc_id FROM external_docs WHERE content_hash=?", (h,)).fetchone()
    if dup:
        return dup["doc_id"]  # already ingested — dedupe

    os.makedirs(EXT_DOCS_DIR, exist_ok=True)
    doc_id = hashlib.sha256(f"{h}{utcnow()}".encode()).hexdigest()[:16]
    dest = os.path.join(EXT_DOCS_DIR, f"{doc_id}{ext}")
    shutil.copyfile(src_path, dest)
    if not title:
        title = os.path.basename(src_path)
    con.execute("""INSERT INTO external_docs (doc_id, title, file_path, content_hash, source_desc, state)
                   VALUES (?,?,?,?,?,'transcript_ready')""",
                (doc_id, title, dest, h, source_desc))
    con.commit()
    return doc_id


def process_document(con, doc_id: str, dry_run: bool = False) -> str:
    """Run the standard brief pipeline on an external doc. Returns brief_id."""
    from ytbrief.brief_worker import (build_prompt, parse_json_loose, validate,
                                      BRIEF_DIR, ANALYSIS_VERSION, new_ulid,
                                      REQUIRED_FOOTER, DELIVERY_TARGET)
    from ytbrief.models import call_llm_with_fallback, notify_fallback
    import subprocess

    doc = con.execute("SELECT * FROM external_docs WHERE doc_id=?", (doc_id,)).fetchone()
    if not doc:
        raise SystemExit(f"doc {doc_id} not found")
    transcript_text = open(doc["file_path"], encoding="utf-8", errors="replace").read()

    video = {
        "video_id": f"DOC-{doc['doc_id'][:8].upper()}",
        "title": doc["title"],
        "channel_title": doc["source_desc"] or "External document",
        "published_at": doc["ingested_at"],
        "transcript_source": "external_document",
        "duration_s": None,
    }
    prompt = build_prompt(video, transcript_text)
    raw, used, failures = call_llm_with_fallback(prompt, video_id=video["video_id"])
    if failures:
        notify_fallback(used, failures, video["video_id"])
    brief = parse_json_loose(raw)

    # footer enforcement + enum normalization (same defense-in-depth as video path)
    _conf = (brief.get("regime_conf") or "").strip().capitalize()
    brief["regime_conf"] = _conf if _conf in ("High", "Medium", "Low") else None
    _reg = (brief.get("regime_analyst") or "").strip()
    brief["regime_analyst"] = _reg if _reg in ("Tightening", "Neutral", "Easing", "Flood", "Mixed-Unclear") else "Mixed-Unclear"
    if doc["source_desc"]:
        brief["report_md"] = (f"*Source: external document — {doc['source_desc']}. "
                              f"Speaker claims may not be verifiable against video timestamps.*\n\n" + brief["report_md"])
    if REQUIRED_FOOTER not in brief.get("card", ""):
        brief["card"] = brief.get("card", "").rstrip() + f"\n\n*{REQUIRED_FOOTER}*"
    if REQUIRED_FOOTER not in brief.get("report_md", ""):
        brief["report_md"] = brief.get("report_md", "").rstrip() + f"\n\n---\n*{REQUIRED_FOOTER}*\n"

    errs = validate(brief)
    # external docs: no real video timestamps exist — allow missing ts_start in claims
    if errs:
        raise ValueError("validation failed: " + "; ".join(errs))

    brief_id = new_ulid()
    os.makedirs(BRIEF_DIR, exist_ok=True)
    report_path = os.path.join(BRIEF_DIR, f"{brief_id}.md")
    header = (f"---\nbrief_id: {brief_id}\ndoc_id: {doc_id}\nanalysis_version: {ANALYSIS_VERSION}\n"
              f"source: external_document\npivotality: {brief.get('pivotality')}\n---\n\n")
    with open(report_path, "w") as f:
        f.write(header + brief["report_md"])

    con.execute("""INSERT INTO briefs (brief_id, video_id, analysis_version, pivotality,
                   regime_speaker, regime_analyst, regime_conf, regime_divergences,
                   report_path, card_text, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (brief_id, video["video_id"], ANALYSIS_VERSION, brief.get("pivotality"),
                 brief.get("regime_speaker"), brief.get("regime_analyst"), brief.get("regime_conf"),
                 brief.get("regime_divergences"), report_path, brief.get("card"), utcnow()))
    # claims reference video_id FK — use the synthetic DOC- id; insert a stub row to satisfy FK
    con.execute("""INSERT INTO videos (video_id, channel_id, title, published_at, discovered_at, state, origin)
                   VALUES (?, (SELECT channel_id FROM channels LIMIT 1), ?, ?, ?, 'analyzed', 'manual')
                   ON CONFLICT(video_id) DO NOTHING""",
                (video["video_id"], doc["title"], doc["ingested_at"], utcnow()))
    for c in brief.get("claims", []):
        claim_id = new_ulid()
        con.execute("""INSERT INTO claims (claim_id, brief_id, video_id, analysis_version,
                       claim_text, quote, ts_start, ts_offset_s, occurred_at, label,
                       claim_type, speaker_role, pivotality)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_id, brief_id, video["video_id"], ANALYSIS_VERSION,
                     c.get("claim_text"), c.get("quote"), c.get("ts_start"),
                     c.get("ts_offset_s"), doc["ingested_at"], c.get("label"),
                     c.get("claim_type"), c.get("speaker_role", "speaker"),
                     c.get("pivotality")))
    con.execute("UPDATE external_docs SET state='analyzed', brief_id=? WHERE doc_id=?", (brief_id, doc_id))
    con.commit()

    if dry_run:
        return brief_id

    r = subprocess.run(["hermes", "send", "--to", DELIVERY_TARGET, brief.get("card", "")],
                       capture_output=True, text=True, timeout=120)
    state = "sent" if r.returncode == 0 else "failed"
    from ytbrief.brief_worker import new_ulid as _nu
    con.execute("""INSERT INTO deliveries (delivery_id, brief_id, target, state, attempts, last_error)
                   VALUES (?,?,?,?,1,?)""",
                (_nu(), brief_id, DELIVERY_TARGET, state, r.stderr[:300] if r.returncode else None))
    if state == "sent":
        con.execute("UPDATE external_docs SET state='delivered' WHERE doc_id=?", (doc_id,))
    con.commit()
    return brief_id


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="path to .txt/.md transcript")
    ap.add_argument("--title", default=None)
    ap.add_argument("--source", default=None, help="e.g. 'podcast transcript, provided by user'")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = connect()
    doc_id = register_document(con, args.path, title=args.title, source_desc=args.source)
    print(f"registered doc: {doc_id}")
    brief_id = process_document(con, doc_id, dry_run=args.dry_run)
    print(f"brief: {brief_id}" + (" (dry-run)" if args.dry_run else " — delivered"))
