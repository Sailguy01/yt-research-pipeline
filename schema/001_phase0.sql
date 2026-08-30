-- Migration 001 — Phase 0 frozen schema (2026-08-29)
-- Design sources: youtube-investment-analysis-plan.md §3/§4,
-- youtube-knowledge-layer-addendum.md §K1 (5-expert panel).
-- Invariants: ULIDs minted once never reused; UTC ISO-8601; platform-native IDs;
-- claim-level extraction from day one; append-only supersession, never mutation.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  note TEXT
);

-- ---------- Channel registry ----------
CREATE TABLE IF NOT EXISTS channels (
  channel_id   TEXT PRIMARY KEY,              -- UC... platform-native
  handle       TEXT,
  title        TEXT,
  enabled      INTEGER NOT NULL DEFAULT 1,
  priority     INTEGER NOT NULL DEFAULT 0,    -- higher = process first
  source       TEXT NOT NULL DEFAULT 'manual',-- manual | dedicated-sync
  topic_rules  TEXT,                          -- JSON allow/deny rules
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_poll_at TEXT,
  etag         TEXT,
  last_error   TEXT,
  last_sync_at TEXT
);

-- ---------- Videos (dedupe + state machine) ----------
CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,            -- platform-native 11-char ID
  channel_id     TEXT NOT NULL REFERENCES channels(channel_id),
  title          TEXT,
  published_at   TEXT,                        -- UTC ISO-8601 (YouTube published)
  discovered_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_s     INTEGER,
  is_short       INTEGER,
  is_live_vod    INTEGER,                     -- 1 once VOD complete
  state          TEXT NOT NULL DEFAULT 'discovered'
                 CHECK (state IN ('discovered','metadata_checked','waiting_vod',
                                  'waiting_captions','transcript_ready','analyzed',
                                  'delivery_pending','delivered','out_of_scope',
                                  'private_or_deleted','unsupported','dead_letter',
                                  'delivery_uncertain')),
  state_reason   TEXT,
  origin         TEXT NOT NULL DEFAULT 'subscription' CHECK (origin IN ('subscription','manual')),
  priority       INTEGER NOT NULL DEFAULT 0,  -- manual links > subscription
  title_history  TEXT                         -- JSON array for reanalysis rules
);
CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state);
CREATE INDEX IF NOT EXISTS idx_videos_channel_pub ON videos(channel_id, published_at);

-- ---------- Assets (canonical + aliases) ----------
CREATE TABLE IF NOT EXISTS assets (
  asset_id   TEXT PRIMARY KEY,                -- canonical: BTC, TSLA, MSTR, SOL, ETH, SPCX
  kind       TEXT NOT NULL CHECK (kind IN ('crypto','equity','private_reference','macro','other')),
  name       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_aliases (
  alias      TEXT PRIMARY KEY,                -- 'Bitcoin','BTC','$BTC','bitcoin' -> BTC
  asset_id   TEXT NOT NULL REFERENCES assets(asset_id)
);

-- ---------- Transcripts ----------
CREATE TABLE IF NOT EXISTS transcripts (
  video_id     TEXT PRIMARY KEY REFERENCES videos(video_id),
  source       TEXT NOT NULL CHECK (source IN ('creator','auto_caption','local_asr')),
  language     TEXT,
  fetched_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  char_count   INTEGER,
  content_hash TEXT NOT NULL,                 -- sha256 for idempotent reprocessing
  file_path    TEXT NOT NULL,                 -- markdown/txt on disk
  asr_model    TEXT                           -- set when source=local_asr
);

-- ---------- Briefs (the human artifact) ----------
CREATE TABLE IF NOT EXISTS briefs (
  brief_id         TEXT PRIMARY KEY,          -- ULID
  video_id         TEXT NOT NULL REFERENCES videos(video_id),
  analysis_version TEXT NOT NULL,             -- schema+skill+model version tag
  pivotality       INTEGER CHECK (pivotality BETWEEN 1 AND 5),
  regime_speaker   TEXT,                      -- claimed by speaker, or 'none stated'
  regime_analyst   TEXT,                      -- Tightening/Neutral/Easing/Flood/Mixed-Unclear
  regime_conf      TEXT CHECK (regime_conf IN ('High','Medium','Low') OR regime_conf IS NULL),
  regime_divergences TEXT,                    -- indicators pointing opposite ways
  report_path      TEXT NOT NULL,             -- full local markdown report
  card_text        TEXT,                      -- Telegram card content
  notified         INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Claims (the queryable distillation — day-one requirement) ----------
CREATE TABLE IF NOT EXISTS claims (
  claim_id        TEXT PRIMARY KEY,           -- ULID
  brief_id        TEXT NOT NULL REFERENCES briefs(brief_id),
  video_id        TEXT NOT NULL REFERENCES videos(video_id),
  analysis_version TEXT NOT NULL,
  claim_text      TEXT NOT NULL,
  quote           TEXT,                       -- verbatim transcript support
  ts_offset_s     INTEGER,                    -- in-transcript offset
  ts_start        TEXT,                       -- video timestamp for links
  occurred_at     TEXT NOT NULL,              -- video publish time (UTC)
  ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  label           TEXT NOT NULL CHECK (label IN ('Claim','Opinion','Forecast','Fact','Verified','Corroborated','Disputed','Unverified','ASR-risk')),
  claim_type      TEXT CHECK (claim_type IN ('thesis','forecast','data_point','regime_call','risk')),
  speaker_role    TEXT NOT NULL DEFAULT 'speaker' CHECK (speaker_role IN ('speaker','analyst')),
  confidence      REAL,
  pivotality      INTEGER CHECK (pivotality BETWEEN 1 AND 5),
  superseded_by   TEXT REFERENCES claims(claim_id)  -- append-only thesis flips
);
CREATE INDEX IF NOT EXISTS idx_claims_video ON claims(video_id);
CREATE INDEX IF NOT EXISTS idx_claims_pivotal ON claims(pivotality);
CREATE INDEX IF NOT EXISTS idx_claims_occurred ON claims(occurred_at);

CREATE TABLE IF NOT EXISTS claim_assets (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  PRIMARY KEY (claim_id, asset_id)
);

-- ---------- Themes (small, append-only controlled vocabulary) ----------
CREATE TABLE IF NOT EXISTS themes (
  theme_id TEXT PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE,
  description TEXT
);
CREATE TABLE IF NOT EXISTS claim_themes (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  theme_id TEXT NOT NULL REFERENCES themes(theme_id),
  PRIMARY KEY (claim_id, theme_id)
);

-- ---------- Theses + contradiction ledger ----------
CREATE TABLE IF NOT EXISTS theses (
  thesis_id   TEXT PRIMARY KEY,               -- ULID
  asset_id    TEXT NOT NULL REFERENCES assets(asset_id),
  thesis_text TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end   TEXT,
  status      TEXT NOT NULL CHECK (status IN ('formed','strengthened','weakened','contradicted','retired')),
  summary     TEXT
);
CREATE TABLE IF NOT EXISTS thesis_events (
  event_id        TEXT PRIMARY KEY,           -- ULID
  asset_id        TEXT NOT NULL REFERENCES assets(asset_id),
  event_ts        TEXT NOT NULL,
  event_type      TEXT NOT NULL CHECK (event_type IN ('reversal','reinforcement','refinement','contradiction')),
  claim_id        TEXT NOT NULL REFERENCES claims(claim_id),
  prior_thesis_id TEXT REFERENCES theses(thesis_id),
  new_thesis_id   TEXT REFERENCES theses(thesis_id),
  note            TEXT
);

-- ---------- Watchlist triggers (conditionals as data) ----------
CREATE TABLE IF NOT EXISTS watchlist (
  asset_id     TEXT PRIMARY KEY REFERENCES assets(asset_id),
  priority     INTEGER NOT NULL DEFAULT 0,
  trigger_conditions TEXT,                    -- JSON
  pivotality   INTEGER CHECK (pivotality BETWEEN 1 AND 5)
);

-- ---------- Deliveries (idempotent Telegram outbox) ----------
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,               -- ULID
  brief_id    TEXT NOT NULL REFERENCES briefs(brief_id),
  target      TEXT NOT NULL,                  -- telegram:<chat>:<thread>
  state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sent','failed','uncertain')),
  tg_message_id INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Digests ----------
CREATE TABLE IF NOT EXISTS digests (
  digest_id      TEXT PRIMARY KEY,            -- ULID
  period_type    TEXT NOT NULL CHECK (period_type IN ('weekly','monthly','quarterly')),
  period_start   TEXT NOT NULL,
  period_end     TEXT NOT NULL,
  generated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  analysis_version_range TEXT NOT NULL,       -- e.g. 'v1..v1'
  conclusion_md  TEXT NOT NULL,
  report_path    TEXT
);

-- ---------- KB chunks (FTS5 + embeddings added in later migration) ----------
CREATE TABLE IF NOT EXISTS kb_chunks (
  chunk_id    TEXT PRIMARY KEY,               -- ULID
  source_type TEXT NOT NULL CHECK (source_type IN ('brief','claim','transcript')),
  source_id   TEXT NOT NULL,
  video_id    TEXT,
  text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON kb_chunks(source_type, source_id);

INSERT OR IGNORE INTO schema_meta (version, note) VALUES (1, 'Phase 0 frozen schema');

-- ---------- Seed canonical watchlist assets ----------
INSERT OR IGNORE INTO assets (asset_id, kind, name) VALUES
 ('BTC','crypto','Bitcoin'),
 ('TSLA','equity','Tesla Inc'),
 ('MSTR','equity','MicroStrategy'),
 ('SOL','crypto','Solana'),
 ('ETH','crypto','Ethereum'),
 ('SPCX','equity','SpaceX (Nasdaq: SPCX, IPO 2026-06-12)');
INSERT OR IGNORE INTO asset_aliases (alias, asset_id) VALUES
 ('Bitcoin','BTC'),('BTC','BTC'),('btc','BTC'),('$BTC','BTC'),
 ('Tesla','TSLA'),('TSLA','TSLA'),('$TSLA','TSLA'),
 ('MicroStrategy','MSTR'),('MSTR','MSTR'),('$MSTR','MSTR'),
 ('Solana','SOL'),('SOL','SOL'),('$SOL','SOL'),
 ('Ethereum','ETH'),('ETH','ETH'),('$ETH','ETH'),
 ('SpaceX','SPCX'),('SPCX','SPCX'),('$SPCX','SPCX');
