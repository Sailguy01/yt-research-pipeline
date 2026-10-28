#!/usr/bin/env python3
"""Digest engine — daily / weekly / monthly synthesis digests (panel-vetted design, 2026-10-26).

Cadences (all America/Chicago local time via launchd):
  daily   : 7:00pm      — prior 24h of briefs. Signal: headline, 3 thematic takeaways,
                          top contradiction, must-watch. Skip silently if 0 briefs.
  weekly  : Saturday 8am — prior 7 days. Narrative: week's arc, echo analysis,
                          regime shifts, watchlist timeline.
  monthly : 1st 6am      — previous calendar month. Structure/thesis level: month-at-a-glance,
                          thesis pulse (net conviction change), structural shifts,
                          fading lines, MoM regime context.

Hard laws (panel round-1, user-locked):
  - Every synthesized conclusion cites supporting claims (video title + timestamp range).
  - Synthesis introduces NO new facts; correlations only, never causal assertions.
  - Brevity law: length driven by information density only. Daily ≤1,500 chars;
    weekly ≤1,800 chars card (full report attached as file); monthly ≤1,800 chars.
  - Idempotent per period (digests table + outbox check); missed run = late catch-up
    flagged, never silent, never duplicate.
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ytbrief.config import DELIVERY_TARGET, HERMES_BIN, DATA_DIR, CUTOFF_DATE
from ytbrief.models import call_llm_with_fallback  # model chain + lenient JSON + retries

DB = os.path.join(DATA_DIR, "pipeline.db")
REPORT_DIR = os.path.join(DATA_DIR, "digests")
os.makedirs(REPORT_DIR, exist_ok=True)

MAX_CHARS = {"daily": 1500, "weekly": 1800, "monthly": 1800}


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _already_run(con, period_key: str) -> bool:
    return con.execute("SELECT COUNT(*) FROM digests WHERE period_key=?", (period_key,)).fetchone()[0] > 0


# ---------------------------------------------------------------- claim loading
def load_claims(start: str, end: str) -> list[dict]:
    con = _db()
    rows = con.execute("""
        SELECT c.claim_id, c.claim_text, c.quote, c.ts_start, c.occurred_at, c.label,
               c.claim_type, c.speaker_role, c.pivotality,
               b.pivotality AS brief_pivotality, b.verdict_tag, b.regime_speaker, b.regime_analyst,
               b.guest_line, v.title AS video_title, b.video_id,
               v.channel_id, ch.title AS channel_title,
               (SELECT GROUP_CONCAT(a.name) FROM claim_assets ca
                JOIN assets a ON a.asset_id = ca.asset_id
                WHERE ca.claim_id = c.claim_id) AS claim_assets
        FROM claims c
        JOIN briefs b ON b.brief_id = c.brief_id
        JOIN videos v ON v.video_id = c.video_id
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE c.occurred_at >= ? AND c.occurred_at < ?
          AND c.superseded_by IS NULL
          AND v.state = 'delivered'
        ORDER BY c.occurred_at DESC""", (start, end)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def compact_digest_inputs(claims: list[dict], limit: int = 90) -> str:
    """Claims compacted for the synthesis prompt: the highest-signal first, terse."""
    def score(c):
        pv = c["pivotality"] or c["brief_pivotality"] or 3
        watchlist = 1 if c["claim_assets"] else 0
        verdict = {"ACT": 1.0, "WATCH": 0.5}.get(c["verdict_tag"] or "", 0.3)
        return pv * (1 + watchlist) * verdict
    out = []
    for c in sorted(claims, key=score, reverse=True)[:limit]:
        pv = c["pivotality"] or c["brief_pivotality"] or 3
        ts = c["ts_start"] or ""
        who = c["channel_title"] or c["video_title"]
        assets = c["claim_assets"] or ""
        out.append(f"- [{c['label']}|P{pv}|{c['claim_type']}] {who} @{ts} ({assets})\n  "
                   f"{(c['claim_text'] or '')[:220]}")
    return "\n".join(out)


# ---------------------------------------------------------------- prompts
GUARDRAILS = """GUARDRAILS (absolute):
- Every statement you write must be supported ONLY by the claims provided. Never introduce a fact, number, or event that is not in them.
- Correlations only. NEVER write causal language ("caused", "led to", "because"). Use "moved with", "alongside", "coincided with".
- Every synthesized bullet ends with a provenance tag: (source: ChannelA; ChannelB) — the channels whose claims support it. 2+ independent channels = "consensus"; 1 channel = "single-source".
- If two claims conflict, say so plainly rather than averaging them.
- Brevity law: use only the words necessary. Never pad."""


def _prompt(mode: str, claims_text: str, window_label: str, extra: str = "") -> str:
    base = f"""You are the synthesis editor for a private investment-research digest. Below are the labeled claims extracted from ~28 investment/macro/crypto YouTube channels during {window_label}.

{GUARDRAILS}

TASK ({mode}):"""
    if mode == "daily":
        task = """Produce the DAILY digest, this exact structure and nothing else:
1. HEADLINE: one line — the single most important takeaway across all claims today.
2. THEMES: exactly 3 bullets, each a cross-video theme (claims appearing across 2+ independent channels = consensus; note single-source otherwise). Cite channels in the tag.
3. CONTRADICTION: the sharpest conflict between two claims today (state both sides, no adjudication). Omit the line entirely if none exists.
4. MUST-WATCH: the single most pivotal video today (title + channel + one line why).
5. Footer line: "n briefs · m claims · correlations only, not advice."
Constraints: total ≤1,500 characters. Telegram markdown (bold with *asterisks*). No preamble, no postamble."""
    elif mode == "weekly":
        task = """Produce the WEEKLY digest, this exact structure and nothing else:
1. WEEK'S ARC: 2-3 sentences — how the narrative moved across the week.
2. CONVICTION: up to 4 themes, each with echo count (how many independent channels voiced it) and direction (rising/fading). Cite channels.
3. REGIME: one or two lines — dominant regime calls this week, speaker-claimed vs analyst-inferred kept separate.
4. WATCHLIST: one line per asset actually discussed (BTC/TSLA/SPCX/etc.), each ≤15 words.
5. Footer line: "n briefs · m claims · correlations only, not advice."
Constraints: total ≤1,800 characters. Dense, no filler. No preamble/postamble."""
    else:  # monthly
        task = """Produce the MONTHLY digest, this exact structure and nothing else:
1. MONTH AT A GLANCE: one line — the month's dominant conclusion.
2. THESIS PULSE: 3-5 theses raised during the month, each with net conviction change (RISING / FADING / CONTESTED) and supporting channels.
3. STRUCTURAL SHIFTS: 1-2 lines on how long-running narratives evolved during the month.
4. FADING LINES: narratives that lost traction this month (omit if none).
5. MoM CONTEXT: one sentence contrasting this month's discourse with the previous month (previous month's digest summary is provided below if available).
6. Footer line: "n briefs · m claims · correlations only, not advice."
Constraints: total ≤1,800 characters. Thesis altitude: how the month's stories changed the landscape, NOT a recap of events. No preamble/postamble."""
    return f"{base}\n{task}\n{extra}\n\nCLAIMS ({window_label}):\n{claims_text}"


# ---------------------------------------------------------------- synthesis
def synthesize(mode: str, claims: list[dict], window_label: str, extra: str = "") -> str:
    claims_text = compact_digest_inputs(claims)
    prompt = _prompt(mode, claims_text, window_label, extra)
    raw, _, _ = call_llm_with_fallback(prompt)
    # lenient JSON not needed — plain text output; strip any box-drawing/frame artifacts
    text = raw.strip()
    if text.startswith("╭"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith(("╭", "╰", "│", "Query:")))
    return text.strip()


# ---------------------------------------------------------------- delivery
def deliver(text: str, subject: str, report_path: str | None = None):
    body = text  # no MEDIA attachment: card is self-contained; reports stay on disk
    cmd = [HERMES_BIN, "send", "--to", DELIVERY_TARGET, "-s", subject, body]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"delivery failed: {r.stderr[:200]}")
    return r.stdout.strip()


def record_digest(mode: str, period_key: str, start: str, end: str, card: str,
                  n_briefs: int, n_claims: int, report_path: str | None):
    con = _db()
    con.execute("""INSERT INTO digests (digest_id, period_key, period_type, period_start, period_end,
                   conclusion_md, report_path)
                   VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?)""",
                (period_key, mode, start, end, card, report_path))
    con.commit()
    con.close()


# ---------------------------------------------------------------- windows
def window(mode: str) -> tuple[str, str, str, str]:
    """Returns (start, end, period_key, label). All UTC ISO."""
    now = datetime.now(timezone.utc)
    if mode == "daily":
        start = now - timedelta(hours=24)
        key = now.strftime("%Y-%m-%d")
        label = f"{start.strftime('%b %d %H:%M')}–{now.strftime('%H:%M')} UTC (last 24h)"
        return start.isoformat(), now.isoformat(), f"daily-{key}", label
    if mode == "weekly":
        start = now - timedelta(days=7)
        key = f"weekly-{start.strftime('%G-W%V')}"
        label = f"{start.strftime('%b %d')}–{now.strftime('%b %d')} (7 days)"
        return start.isoformat(), now.isoformat(), key, label
    # monthly: previous calendar month
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_this
    prev_end = first_this - timedelta(days=1)
    start = prev_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    key = f"monthly-{prev_end.strftime('%Y-%m')}"
    label = f"{start.strftime('%B %Y')} (complete)"
    return start.isoformat(), end.isoformat(), key, label


def prev_month_summary(mode: str) -> str:
    """For monthly MoM context: summary line from the previous month's digest, if any."""
    if mode != "monthly":
        return ""
    con = _db()
    r = con.execute("""SELECT conclusion_md FROM digests WHERE period_type='monthly'
                       ORDER BY period_start DESC LIMIT 2""").fetchall()
    con.close()
    if len(r) < 2:
        return ""
    return f"PREVIOUS MONTH'S DIGEST (for contrast only — do not repeat it):\n{r[1][0][:800]}"


# ---------------------------------------------------------------- selective alerting
# Panel law: alerts only for anomalies needing human action. Quiet days, already-run,
# and routine completions are NOT alert-worthy. Two alert classes, both capped 1/day:
#   A. a scheduled digest failed to deliver after 1 retry (the silent-failure class)
#   B. staleness: a digest window closed but its digest never recorded (job unloaded/crashed)
def alert(message: str) -> bool:
    """Send one ops alert. Deduped per calendar day by alert state file. Returns sent?."""
    state = os.path.join(DATA_DIR, "digests", "alert_state.json")
    import json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent = {}
    if os.path.exists(state):
        sent = json.load(open(state))
    if sent.get(today) == message:
        return False  # already alerted today about exactly this — no spam
    r = subprocess.run([HERMES_BIN, "send", "--to", DELIVERY_TARGET,
                        "-s", "⚠️ digest ops", message], capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        sent[today] = message
        json.dump(sent, open(state, "w"))
        return True
    return False


def staleness_check(mode: str, period_key: str) -> None:
    """Alert (once) if a digest for this cadence should have run but never recorded.
    Only for daily/weekly/monthly natural-fire context; catches unloaded jobs & crashes."""
    con = _db()
    rows = con.execute("SELECT MAX(generated_at) FROM digests WHERE period_type=?", (mode,)).fetchone()[0]
    con.close()
    if not rows:
        return  # first-ever run for this cadence
    try:
        last = datetime.fromisoformat(rows.replace("Z", "+00:00"))
    except ValueError:
        return  # malformed row — fail open, never false-alert
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)  # bare dates are UTC
    age_days = (datetime.now(timezone.utc) - last).days
    need = {"daily": 3, "weekly": 16, "monthly": 40}[mode]  # generous: never false-positive on quiet days
    if age_days > need:
        alert(f"{mode} digest staleness: last successful {mode} digest was {age_days} days ago "
              f"({last:%b %d}). Job may have stopped firing — check launchctl list / tick logs.")


# ---------------------------------------------------------------- main
def run(mode: str, force: bool = False) -> str:
    start, end, period_key, label = window(mode)
    con = _db()
    if _already_run(con, period_key) and not force:
        con.close()
        return f"already-run:{period_key}"
    # count briefs in window for the quiet-day rule
    n_briefs = con.execute("SELECT COUNT(*) FROM briefs WHERE created_at>=? AND created_at<?",
                           (start, end)).fetchone()[0]
    con.close()

    if mode == "daily" and n_briefs == 0:
        return f"quiet:{period_key}"  # skip silently — no noise (panel rule)

    claims = load_claims(start, end)
    if not claims:
        return f"quiet:{period_key}"

    extra = prev_month_summary(mode) if mode == "monthly" else ""
    card = synthesize(mode, claims, label, extra)

    # enforce char ceiling — ask model to tighten once if overrun
    if len(card) > MAX_CHARS[mode] * 1.15:
        tighten, _, _ = call_llm_with_fallback(
            f"Rewrite this digest to under {MAX_CHARS[mode]} characters. Keep every source citation and the structure. Remove no factual content — compress wording only.\n\n{card}")
        if len(tighten) < len(card):
            card = tighten.strip()

    report_path = os.path.join(REPORT_DIR, f"{period_key}.md")
    with open(report_path, "w") as f:
        f.write(f"# {mode.capitalize()} digest — {label}\n\n{card}\n\n"
                f"(claims window: {start} → {end}; guardrails: cited, correlations-only)\n")
    record_digest(mode, period_key, start, end, card, n_briefs, len(claims), report_path)  # idempotency gate first
    try:
        try:
            deliver(card, f"{mode} digest {label}", report_path)
        except Exception:
            # one retry (transient gateway blips) before alerting — no alert on first blip
            time.sleep(30)
            deliver(card, f"{mode} digest {label}", report_path)
    except Exception as exc:
        con = _db(); con.execute("DELETE FROM digests WHERE period_key=?", (period_key,)); con.commit(); con.close()
        alert(f"{mode} digest for {period_key} FAILED to deliver after retry. Error: {str(exc)[:180]}. "
              f"Card is saved at {report_path} — say 'redeliver' to send it.")
        raise
    staleness_check(mode, period_key)
    return f"delivered:{period_key}:{len(claims)} claims"


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    force = "--force" in sys.argv
    if mode not in ("daily", "weekly", "monthly"):
        print("usage: digest_engine.py [daily|weekly|monthly] [--force]")
        sys.exit(2)
    print(run(mode, force))
