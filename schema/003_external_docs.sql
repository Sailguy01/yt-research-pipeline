-- Migration 003 — external (non-YouTube) transcript documents
-- User can post .txt/.md transcripts in Telegram; these flow through the same
-- brief pipeline (skill → claims → card → delivery) keyed by document, not video.
CREATE TABLE IF NOT EXISTS external_docs (
  doc_id      TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  file_path   TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  source_desc TEXT,
  ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  state       TEXT NOT NULL DEFAULT 'discovered'
              CHECK (state IN ('discovered','transcript_ready','analyzed','delivery_pending','delivered','dead_letter')),
  brief_id    TEXT
);
INSERT OR IGNORE INTO schema_meta (version, note) VALUES (3, '003_external_docs.sql');
