"""Central configuration — single source of truth for every environment-specific value.

Resolution priority: env (YTREPO_*) > derived default.
Derived defaults are repo-root-relative (Path(__file__)-anchored) so they are
byte-identical to the pre-migration literals on this machine and portable elsewhere.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()

# --- Binaries (absolute paths required: launchd has no shell PATH) ---
HERMES_BIN = os.environ.get("YTREPO_HERMES_BIN", str(HOME / ".local" / "bin" / "hermes"))
YTDLP_BIN = os.environ.get("YTREPO_YTDLP_BIN", str(REPO_ROOT / ".venv" / "bin" / "yt-dlp"))
NODE_BIN = os.environ.get("YTREPO_NODE_BIN",
    str(HOME / ".hermes" / "tools" / "node-26.7.0-darwin-arm64" / "bin" / "node"))
WHISPER_BIN = os.environ.get("YTREPO_WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("YTREPO_WHISPER_MODEL",
    str(REPO_ROOT / "data" / "models" / "ggml-medium.bin"))

# --- Paths ---
DATA_DIR = os.environ.get("YTREPO_DATA_DIR", str(REPO_ROOT / "data"))
DB_PATH = str(Path(DATA_DIR) / "pipeline.db")

# --- Telegram delivery (single source; mild metadata — real value supplied via env in production) ---
DELIVERY_TARGET = os.environ.get("YTREPO_DELIVERY_TARGET", "telegram:CHAT_ID:TOPIC_ID")

# --- Keychain (labels only; the token itself NEVER leaves Keychain) ---
KEYCHAIN_SERVICE = "yt-research-pipeline-oauth"
KEYCHAIN_ACCOUNT = "dedicated-ai-account"

# --- Automation constants (unchanged behavior) ---
CUTOFF_DATE = os.environ.get("YTREPO_CUTOFF", "2026-10-08")
MONTHLY_BUDGET = float(os.environ.get("YTREPO_MONTHLY_BUDGET", "5.00"))


def self_check() -> list[str]:
    """Assert every resolved binary exists and is executable. Returns problems."""
    problems = []
    for name, path in [("HERMES_BIN", HERMES_BIN), ("YTDLP_BIN", YTDLP_BIN),
                       ("NODE_BIN", NODE_BIN), ("WHISPER_BIN", WHISPER_BIN)]:
        if not os.path.isfile(path):
            problems.append(f"{name} missing: {path}")
        elif not os.access(path, os.X_OK):
            problems.append(f"{name} not executable: {path}")
    if not os.path.isfile(WHISPER_MODEL):
        problems.append(f"WHISPER_MODEL missing: {WHISPER_MODEL}")
    return problems


if __name__ == "__main__":
    vals = {k: v for k, v in globals().items() if k.isupper()}
    for k, v in sorted(vals.items()):
        print(f"{k:20} {v}")
    probs = self_check()
    print("\nself-check:", "OK" if not probs else probs)
