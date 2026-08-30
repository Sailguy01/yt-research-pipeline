-- Migration 004 — card v3 fields
ALTER TABLE briefs ADD COLUMN verdict_tag TEXT;
ALTER TABLE briefs ADD COLUMN verdict_line TEXT;
ALTER TABLE briefs ADD COLUMN guest_line TEXT;
INSERT OR IGNORE INTO schema_meta (version, note) VALUES (4, '004_brief_v3_columns.sql');
