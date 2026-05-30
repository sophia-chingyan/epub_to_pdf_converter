"""Application configuration, loaded from environment variables.

Nothing here raises on import even if OAuth credentials are missing, so the
app and its tests can be imported in any environment. Missing credentials only
cause an error if/when the Google sign-in flow is actually exercised.
"""
from __future__ import annotations

import os
from pathlib import Path


def _csv(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


# --- Auth -------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# Comma-separated allowlist of Google account emails permitted to sign in.
# For a single-user deployment this is just your own address.
ALLOWED_EMAILS = _csv(os.getenv("ALLOWED_EMAILS", ""))

# Public base URL of the deployment, e.g. https://my-app.zeabur.app
# Used to build the OAuth redirect URI ({BASE_URL}/auth).
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Secret used to sign session cookies. MUST be set to a long random value
# in production.
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-change-me")

# --- Storage ----------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "tmp" / "uploads"
JOB_DIR = DATA_DIR / "tmp" / "jobs"
LIBRARY_DIR = DATA_DIR / "library"

# --- Conversion -------------------------------------------------------------
# Path to the system Chromium/Chrome binary used by Vivliostyle.
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "/usr/bin/chromium")

# Default page size for REFLOWABLE books (fixed-layout books keep their own
# page geometry). Vivliostyle presets: A5, A4, A3, B5, B4, JIS-B5, JIS-B4,
# letter, legal, ledger; or a custom value like "182mm,257mm".
REFLOWABLE_PAGE_SIZE = os.getenv("REFLOWABLE_PAGE_SIZE", "A5")

# Per-job timeout in seconds (passed to Vivliostyle and enforced on the
# subprocess).
JOB_TIMEOUT_SEC = int(os.getenv("JOB_TIMEOUT_SEC", "300"))

# Maximum accepted upload size in megabytes.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Cover thumbnail width in pixels (aspect ratio preserved).
COVER_THUMB_WIDTH = int(os.getenv("COVER_THUMB_WIDTH", "200"))

# How many recent books to show on the convert page.
RECENT_COUNT = int(os.getenv("RECENT_COUNT", "10"))


def ensure_dirs() -> None:
    """Create the runtime directory tree if it does not exist."""
    for d in (UPLOAD_DIR, JOB_DIR, LIBRARY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def https_only() -> bool:
    return BASE_URL.startswith("https://")
