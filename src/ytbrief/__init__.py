"""YouTube research pipeline — shared library."""
import datetime
import sqlite3
import os

HOME = os.path.expanduser("~")
PROJECT = f"{HOME}/yt-research-pipeline"
DB_PATH = f"{PROJECT}/data/pipeline.db"
DATA_DIR = f"{PROJECT}/data"

SCHEMA_MIGRATIONS = ["001_phase0.sql", "002_channels_last_sync.sql",
                     "003_external_docs.sql", "004_brief_v3_columns.sql"]


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    os.makedirs(f"{DATA_DIR}/transcripts", exist_ok=True)
    os.makedirs(f"{DATA_DIR}/briefs", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def migrate(con: sqlite3.Connection):
    """Apply pending schema migrations (idempotent)."""
    con.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        note TEXT)""")
    done = {r[0] for r in con.execute("SELECT version FROM schema_meta")}
    for i, fname in enumerate(SCHEMA_MIGRATIONS, start=1):
        if i in done:
            continue
        sql = open(f"{PROJECT}/schema/{fname}").read()
        con.executescript(sql)
        con.execute("INSERT OR IGNORE INTO schema_meta (version, note) VALUES (?,?)",
                    (i, fname))
        con.commit()


def to_cst(iso_utc: str) -> str:
    """Convert an ISO-8601 UTC timestamp to US Central (CDT/CST auto)."""
    import datetime as _dt
    try:
        dt = _dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return iso_utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        central = dt.astimezone(ZoneInfo("America/Chicago"))
    except Exception:
        central = dt.astimezone(_dt.timezone(_dt.timedelta(hours=-5)))
    return central.strftime("%Y-%m-%d %H:%M CT")
