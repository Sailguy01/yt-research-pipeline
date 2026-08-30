-- Migration 006 — cost tracking per brief
ALTER TABLE briefs ADD COLUMN input_tokens INTEGER;
ALTER TABLE briefs ADD COLUMN output_tokens INTEGER;
ALTER TABLE briefs ADD COLUMN cost_usd REAL;
ALTER TABLE briefs ADD COLUMN model_label TEXT;
INSERT OR IGNORE INTO schema_meta (version, note) VALUES (6, '006_brief_cost_columns.sql');
