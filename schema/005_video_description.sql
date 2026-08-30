-- Migration 005 — video description (for guest identification)
ALTER TABLE videos ADD COLUMN description TEXT;
INSERT OR IGNORE INTO schema_meta (version, note) VALUES (5, '005_video_description.sql');
