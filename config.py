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


def _platform_base_url() -> str:
    """Public URL supplied by the hosting platform, if it exposes one.

    Railway injects RAILWAY_PUBLIC_DOMAIN (e.g. "my-app-production.up.railway.app")
    for every service that has a domain, so BASE_URL does not have to be set by
    hand on a first deploy. An explicit BASE_URL always wins — use it when the
    service is reached through a custom domain.
    """
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    # Older Railway builds expose the full URL rather than the bare domain.
    static_url = os.getenv("RAILWAY_STATIC_URL", "").strip()
    if static_url:
        return static_url if "://" in static_url else f"https://{static_url}"
    return ""


# Public base URL of the deployment, e.g. https://my-app.up.railway.app
# Used to build the OAuth redirect URI ({BASE_URL}/auth).
BASE_URL = (
    os.getenv("BASE_URL", "").strip()
    or _platform_base_url()
    or "http://localhost:8000"
).rstrip("/")

# Secret used to sign session cookies. MUST be set to a long random value
# in production.
INSECURE_SESSION_SECRET = "dev-insecure-change-me"
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip() or INSECURE_SESSION_SECRET

# Set by Railway on every deployment; used only to tailor startup warnings.
ON_RAILWAY = bool(
    os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT")
)

# --- Storage ----------------------------------------------------------------
# Where uploads, scratch space, and the PDF library live. This must be a
# persistent volume in production; on a plain container filesystem the library
# is wiped by every redeploy. Railway exposes an attached volume's mount point
# as RAILWAY_VOLUME_MOUNT_PATH, which is used when DATA_DIR is not set.
VOLUME_MOUNT_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
DATA_DIR = Path(
    os.getenv("DATA_DIR", "").strip() or VOLUME_MOUNT_PATH or "./data"
).resolve()
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

# --- Chunked rendering (large-book resilience) -----------------------------
# Maximum number of spine items (chapters) per rendering chunk. Books with
# more spine items than this are split into chunks, each rendered to a
# separate PDF and then merged. Set to 0 to disable chunking.
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "50"))

# How many times to retry a failed chunk render before giving up.
# The initial attempt always runs; this controls additional retries on
# transient errors (total attempts = 1 + CHUNK_MAX_RETRIES).
CHUNK_MAX_RETRIES = int(os.getenv("CHUNK_MAX_RETRIES", "2"))

# Adaptive per-chunk timeout = base + (spine items in chunk) * per_item.
ADAPTIVE_TIMEOUT_BASE = int(os.getenv("ADAPTIVE_TIMEOUT_BASE", "60"))
ADAPTIVE_TIMEOUT_PER_SPINE_ITEM = int(
    os.getenv("ADAPTIVE_TIMEOUT_PER_SPINE_ITEM", "10")
)

# --- OCR / Text Layer -------------------------------------------------------
# Controls when OCR is applied to rebuild the text layer.
# "auto" = detect PUA-obfuscated text and OCR only those books.
# "always" = always run OCR on every converted PDF.
# "off" = never run OCR (fastest; text layer may be PUA gibberish).
TEXT_LAYER_MODE = os.getenv("TEXT_LAYER_MODE", "auto").lower()

# Tesseract language string for OCR (joined with "+"). Only horizontal models
# are listed here because they are installed via apt in the Dockerfile. The
# vertical models (chi_tra_vert, jpn_vert) are added automatically for
# vertical (rtl) books *when their data is installed* — see
# converter.add_text_layer / resolve_ocr_langs. Any requested model that is not
# installed is dropped at runtime rather than aborting the whole OCR pass.
OCR_LANGS = os.getenv("OCR_LANGS", "chi_tra+chi_sim+jpn+kor+eng")

# Number of parallel OCR workers (passed to ocrmypdf --jobs). Defaults to the
# detected CPU count, capped at 4 to keep memory bounded on small containers.
OCR_JOBS = int(os.getenv("OCR_JOBS", str(min(4, os.cpu_count() or 2))))

# Fraction of extracted characters in PUA ranges that triggers OCR in "auto"
# mode. Range 0.0–1.0; default 0.20 means if ≥20% of sampled characters are
# PUA, the text layer is considered obfuscated.
PUA_THRESHOLD = float(os.getenv("PUA_THRESHOLD", "0.20"))

# How many recent books to show on the convert page.
RECENT_COUNT = int(os.getenv("RECENT_COUNT", "10"))

# --- PDF → ePUB ---------------------------------------------------------------
# OCR for PDF input: "auto" repairs scanned PDFs (no text layer) and
# PUA-obfuscated text layers with ocrmypdf before extraction; "off" converts
# whatever text layer exists (scanned pages become images).
PDF_OCR_MODE = os.getenv("PDF_OCR_MODE", "auto").lower()

# Detect ruled tables and emit them as <table> (horizontal-text PDFs only).
PDF_TABLES = os.getenv("PDF_TABLES", "1").lower() not in ("0", "false", "no", "off")

# Rasterise vector drawings (charts, diagrams) into PNG figures.
PDF_DRAWINGS = os.getenv("PDF_DRAWINGS", "1").lower() not in ("0", "false", "no", "off")


def ensure_dirs() -> None:
    """Create the runtime directory tree if it does not exist."""
    for d in (UPLOAD_DIR, JOB_DIR, LIBRARY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def https_only() -> bool:
    return BASE_URL.startswith("https://")


def startup_warnings() -> list[str]:
    """Deployment problems worth shouting about, as human-readable lines.

    These are logged at startup rather than raised: a half-configured instance
    should still boot and serve its login page so the operator can see it came
    up, read the log, and fix the variables.
    """
    warnings: list[str] = []

    if SESSION_SECRET == INSECURE_SESSION_SECRET:
        warnings.append(
            "SESSION_SECRET is unset, so the built-in development value is in "
            "use. Session cookies can be forged — set SESSION_SECRET to a long "
            'random value: python -c "import secrets; print(secrets.token_hex(32))"'
        )

    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        warnings.append(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset, so Google "
            "sign-in will fail. Nobody can use the app until they are set."
        )

    if not ALLOWED_EMAILS:
        warnings.append(
            "ALLOWED_EMAILS is empty, so every sign-in is denied (fail closed). "
            "Set it to your own Google address."
        )

    if not https_only() and not BASE_URL.startswith("http://localhost"):
        warnings.append(
            f"BASE_URL is {BASE_URL!r}, which is not https. Google OAuth will "
            "reject the redirect URI and session cookies will not be marked "
            "Secure."
        )

    if VOLUME_MOUNT_PATH:
        mount = Path(VOLUME_MOUNT_PATH).resolve()
        if DATA_DIR != mount and mount not in DATA_DIR.parents:
            warnings.append(
                f"DATA_DIR ({DATA_DIR}) is outside the mounted volume ({mount}), "
                "so the converted library will be lost on the next redeploy. "
                f"Either set DATA_DIR={mount} or mount the volume at {DATA_DIR}."
            )
    elif ON_RAILWAY:
        warnings.append(
            f"No volume is attached, so the library in {DATA_DIR} lives on the "
            "container filesystem and is wiped by every redeploy and restart. "
            f"Attach a Railway volume mounted at {DATA_DIR}."
        )

    return warnings
