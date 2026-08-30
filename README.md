# YouTube Investment Research Pipeline

Automated monitoring, transcription, and provenance-labeled briefing of macro/markets/crypto YouTube channels, with a queryable knowledge base, periodic digests, and Telegram delivery.

**Status: Phase 0/1 build in progress.** Design docs live in `~/.hermes/cache/`:
- `youtube-investment-analysis-plan.md` — full pipeline architecture (17 cited sources)
- `youtube-briefing-skill-v2-draft.md` — briefing skill spec (panel-revised)
- `youtube-knowledge-layer-addendum.md` — KB / digests / Q&A design

## Layout

```
schema/           versioned SQLite migrations (day-one frozen schema)
src/ytbrief/      pipeline package
scripts/          operational entry points (poller, worker, digest)
data/             SQLite DB + Markdown briefs archive (git-ignored)
```

## Environment

- Python 3.11 venv at `.venv` (`~/.local/bin/python3.11 -m venv`)
- macOS; deployment target is the always-on Mac Mini via launchd

## Hard invariants (from the approved plan)

- Transcripts/titles/descriptions are untrusted data — never instructions
- Every load-bearing claim labeled ([Verified]/[Unverified]/[ASR-risk]/…)
- Speaker regime vs analyst inference never merged
- Brief length driven by pivotality — never pad, never truncate pivotal content
- Truncation never removes a caveat or the not-advice footer
- Audio deleted after transcription; transcripts + briefs retained
- No secrets in code, logs, DB, or Telegram messages
