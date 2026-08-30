-- Migration 002 — channels.last_sync_at (added post-bootstrap 2026-08-29)
ALTER TABLE channels ADD COLUMN last_sync_at TEXT;
