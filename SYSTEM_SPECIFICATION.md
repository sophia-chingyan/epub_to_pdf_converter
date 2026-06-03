# System Specification — ePUB → PDF Converter

**Version:** 1.0  
**Last updated:** 2026-05-31  
**Repository:** `sophia-chingyan/epub_to_pdf_converter`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [System Overview](#2-system-overview)
3. [Architecture](#3-architecture)
4. [Technology Stack](#4-technology-stack)
5. [Module Descriptions](#5-module-descriptions)
6. [API Endpoints](#6-api-endpoints)
7. [Authentication and Authorisation](#7-authentication-and-authorisation)
8. [Conversion Pipeline](#8-conversion-pipeline)
9. [Job Management](#9-job-management)
10. [Library Management](#10-library-management)
11. [Data Storage](#11-data-storage)
12. [Configuration Reference](#12-configuration-reference)
13. [User Interface](#13-user-interface)
14. [Deployment](#14-deployment)
15. [Security Considerations](#15-security-considerations)
16. [Known Limitations](#16-known-limitations)

---

## 1. Purpose and Scope

This application is a **private, single-user web service** that converts ePUB e-books into PDF documents with high fidelity. It is designed for personal use and is restricted to a configurable email allowlist via Google OAuth.

### Primary Goals

- Convert reflowable and fixed-layout ePUBs to PDF.
- Preserve document structure: images, hyperlinks, paragraph styles, table of contents (as PDF bookmarks), and vertical/horizontal CJK typesetting.
- Provide first-class support for **CJK scripts** (Traditional/Simplified Chinese, Japanese with ruby/furigana, Korean) and English.
- Handle very large books reliably through chunked, retried rendering.
- Offer a simple browser-based UI with drag-and-drop upload, live progress display, and a persistent library of converted PDFs.

### Out of Scope

- Multi-user tenancy or per-user libraries.
- Stripping or circumventing DRM (DRM-protected files are detected and rejected).
- Conversion from formats other than ePUB.

---

## 2. System Overview

```
User's Browser
      │
      │  HTTPS
      ▼
┌──────────────────────────────────┐
│  FastAPI web server (app.py)     │
│  ┌──────────┐  ┌───────────────┐ │
│  │  Auth    │  │  Jinja2 UI    │ │
│  │ (auth.py)│  │  (templates/) │ │
│  └──────────┘  └───────────────┘ │
│  ┌────────────────────────────┐  │
│  │  Job Manager (jobs.py)     │  │
│  │  ┌──────────────────────┐  │  │
│  │  │  Converter           │  │  │
│  │  │  (converter.py)      │  │  │
│  │  │  └─ vivliostyle CLI  │  │  │
│  │  │       └─ Chromium    │  │  │
│  │  └──────────────────────┘  │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  Library (library.py)      │  │
│  └────────────────────────────┘  │
└──────────────────────────────────┘
             │
             │  Filesystem I/O
             ▼
    /data/  (persistent volume)
    ├── tmp/uploads/    (ephemeral)
    ├── tmp/jobs/       (ephemeral)
    └── library/        (permanent)
```

---

## 3. Architecture

### Design Principles

| Principle | How it is applied |
|---|---|
| Single-user, single-process | One conversion runs at a time; `uvicorn` is launched with `--workers 1` |
| Simplicity | No database; job state is in memory; library state is on disk |
| Resilience for large books | Chunked rendering with per-chunk retry and adaptive timeouts |
| Security | Email allowlist, signed session cookies, path-traversal guards on all file operations |
| Portability | Everything ships inside a single Docker image |

### Component Interaction Flow

```
Browser
  │ 1. GET /          (renders index.html)
  │ 2. POST /upload   (streams ePUB to UPLOAD_DIR)
  │ 3. POST /start-convert/{filename}
  │        → JobManager.start() → background thread
  │ 4. GET /job-status/{job_id}   (polls every 2 s)
  │        ← JSON progress
  │ 5. GET /download/{name}       (after status = "done")
```

---

## 4. Technology Stack

### Runtime Environment

| Component | Version / Details |
|---|---|
| Python | 3.12 (slim-bookworm base image) |
| Node.js | 20 (required by Vivliostyle CLI) |
| Chromium | System package (`chromium`) — headless renderer |
| Noto CJK fonts | `fonts-noto-cjk`, `fonts-noto-cjk-extra`, `fonts-noto-core` |

### Python Dependencies (`requirements.txt`)

| Package | Purpose |
|---|---|
| `fastapi>=0.110` | HTTP framework and routing |
| `uvicorn[standard]>=0.27` | ASGI server |
| `jinja2>=3.1` | HTML templating |
| `python-multipart>=0.0.9` | Multipart form / file upload parsing |
| `authlib>=1.3` | Google OAuth 2.0 / OpenID Connect client |
| `itsdangerous>=2.1` | Signed session cookie support (via Starlette) |
| `httpx>=0.27` | Async HTTP client (required by Authlib) |
| `pillow>=10.2` | Cover image decoding and thumbnail generation |
| `pypdf>=4.0` | Merging per-chunk PDFs into a single output PDF |

### Front-End

| Component | Details |
|---|---|
| Pico CSS v2 | CDN-loaded CSS framework; provides semantic, classless base styles |
| Vanilla JavaScript | Drag-and-drop upload, progress polling, dynamic step rendering |

---

## 5. Module Descriptions

### `app.py` — HTTP layer

Entry point for the FastAPI application. Declares all routes, enforces authentication on every non-login endpoint, streams uploaded files to disk chunk-by-chunk (with size enforcement), and delegates business logic to the other modules.

Startup hook (`_sweep_temp`) cleans up any orphaned upload or job scratch files left by a previous crash.

### `auth.py` — Authentication helpers

Configures the Authlib Google OAuth client and exposes three helpers:

- `current_user(request)` — returns the session user dict or `None`.
- `is_allowed(email)` — checks the email against `config.ALLOWED_EMAILS`.
- `redirect_uri()` — constructs `{BASE_URL}/auth`.

### `config.py` — Configuration

Reads all configuration from environment variables at import time. Provides `ensure_dirs()` (creates the three runtime directories) and `https_only()` (sets the `https_only` flag on session cookies).

All values have sensible defaults so the app can be imported in any environment without raising.

### `converter.py` — ePUB inspection and PDF rendering

The core domain logic module. Intentionally dependency-light: ePUB parsing uses only the standard-library `zipfile` and `xml.etree.ElementTree`; Pillow is used only for cover thumbnailing.

Responsibilities:
- **Validation** (`validate`) — checks ZIP structure, mimetype, and rejects content DRM.
- **Metadata extraction** (`extract_info`) — reads title, layout type, page direction, and cover image from the OPF manifest.
- **Cover thumbnailing** (`_thumbnail`) — downscales cover images to `COVER_THUMB_WIDTH` pixels using Pillow; SVGs and unreadable images are stored as-is.
- **Vivliostyle command construction** (`build_vivliostyle_cmd`) — pure function that assembles the CLI argument list, making it independently testable.
- **Single-pass rendering** (`render_pdf`) — invokes Vivliostyle as a subprocess.
- **Chunked rendering** (`render_pdf_chunked`) — for large books: splits the spine into chunks, creates sub-EPUBs, renders each chunk, merges results with pypdf.
- **Retry logic** (`_render_with_retry`) — exponential back-off retry with non-retryable error detection.
- **Filename sanitisation** (`safe_filename`) — strips unsafe characters, preserves Unicode word characters (including CJK), limits to 180 chars.

### `jobs.py` — Job management

Implements a single-slot, in-memory job queue (`JobManager`) backed by a `threading.Lock` and a daemon thread.

`Job` dataclass fields: `id`, `display_name`, `status` (`running`|`done`|`error`), `current_step`, `current_label`, `steps` (completed step log), `error`, `output_name`.

`JobManager` exposes:
- `start(upload_path, display_name)` — creates a job, starts the worker thread, returns `job_id`.
- `get(job_id)` — thread-safe job lookup.
- `is_busy()` — returns `True` if a job with `status == "running"` exists.

After a successful conversion the PDF, cover thumbnail, and `.meta.json` sidecar are moved from the job workdir into the library. The workdir and the upload file are always cleaned up in the `finally` block.

### `library.py` — PDF library

Reads and writes the permanent library directory. Each book is represented by three files with a shared stem:

| File | Contents |
|---|---|
| `<stem>-epub-to-pdf.pdf` | Converted PDF |
| `<stem>-epub-to-pdf.cover.<ext>` | Cover thumbnail (optional) |
| `<stem>-epub-to-pdf.meta.json` | JSON sidecar: `title`, `pdf`, `cover`, `fixed_layout` |

Key functions:
- `list_books(limit)` — returns books sorted newest-first; reads sidecar for title and cover filename.
- `pdf_path(name)` / `cover_path(name)` — resolve a filename to a safe absolute path, refusing directory traversal.
- `delete_book(pdf_name)` — deletes the PDF, sidecar, and all cover files matching the stem.
- `delete_all()` — iterates all PDFs and calls `delete_book`.

### `templates/` — Jinja2 HTML templates

| Template | Purpose |
|---|---|
| `login.html` | Sign-in landing page with "Sign in with Google" button |
| `login_error.html` | Shown when sign-in fails or the account is not authorised |
| `index.html` | Main convert page: drag-and-drop zone, confirm panel, live progress view, recently converted books |
| `library.html` | Full library grid with cover thumbnails, download links, and delete controls |

---

## 6. API Endpoints

All endpoints except `/login` and `/auth` require an authenticated session (return `401` otherwise).

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | ✅ | Convert page (or login page if signed out) |
| `GET` | `/library` | ✅ | Library page |
| `GET` | `/login` | — | Redirect to Google OAuth authorisation URL |
| `GET` | `/auth` | — | OAuth callback; validates token, checks allowlist, sets session |
| `GET` | `/logout` | — | Clears session, redirects to `/` |
| `POST` | `/upload` | ✅ | Stream-upload an `.epub`; returns `{"filename": "<safe_name>"}` |
| `POST` | `/start-convert/{filename}` | ✅ | Start a conversion job; returns `{"job_id": "<hex>"}` |
| `GET` | `/job-status/{job_id}` | ✅ | Poll job progress; returns `Job.to_dict()` |
| `GET` | `/download/{name}` | ✅ | Download a converted PDF |
| `GET` | `/cover/{name}` | ✅ | Serve a cover thumbnail |
| `POST` | `/delete/{name}` | ✅ | Delete one book (PDF + sidecar + cover) |
| `POST` | `/delete-all` | ✅ | Delete all books; returns list of deleted filenames |

### Upload constraints

- Accepts only `.epub` files (checked by extension on the server).
- Maximum file size: `MAX_UPLOAD_MB` (default 100 MB), enforced per-chunk during streaming (file is discarded and `400` is returned if exceeded).

### Job status response schema

```json
{
  "status": "running | done | error",
  "current_step": 4,
  "current_label": "Rendering chunk 2/3",
  "steps": [
    {"step": 1, "message": "ePUB validated"},
    {"step": 2, "message": "\"Book Title\""},
    {"step": 3, "message": "reflowable layout detected"}
  ],
  "error": "",
  "output_name": null
}
```

When `status` is `"done"`, `output_name` holds the PDF filename for use with `/download/{name}`.

---

## 7. Authentication and Authorisation

### Flow

1. User visits `/login` → server calls `oauth.google.authorize_redirect()` → browser is redirected to Google.
2. Google redirects to `/auth?code=…` → server calls `oauth.google.authorize_access_token()`.
3. The email from `userinfo` is checked against `ALLOWED_EMAILS` (case-insensitive).
4. On success, `{"email", "name", "picture"}` is stored in the signed session cookie.
5. On failure (network error or disallowed email), `login_error.html` is rendered.

### Session

Sessions use Starlette's `SessionMiddleware` with a signed (HMAC) cookie backed by `SESSION_SECRET`. `https_only` is set to `True` when `BASE_URL` starts with `https://`, preventing the cookie from being sent over plain HTTP.

### Allowlist semantics

`is_allowed` fails closed: an empty `ALLOWED_EMAILS` list denies every user. Only exact case-insensitive email matches are permitted.

---

## 8. Conversion Pipeline

Each job runs the following pipeline in a background daemon thread:

| Step | Label | Action |
|---|---|---|
| 1 | Validating ePUB | `converter.validate()` — checks ZIP, mimetype, `container.xml`; detects content DRM |
| 2 | Extracting metadata & cover | `converter.extract_info()` — reads OPF for title, layout, page direction, cover image |
| 3 | Preparing Vivliostyle | Determines layout mode (fixed/reflowable) for the render command |
| 4 | Rendering PDF | `converter.render_pdf_chunked()` — invokes Vivliostyle (see §8.1) |
| 5 | Checking text layer | `converter.detect_pua_text()` — samples PDF text for PUA obfuscation (auto mode only) |
| 6 | Rebuilding text layer via OCR | `converter.add_text_layer()` — runs ocrmypdf to replace PUA text with real Unicode (see §8.4) |
| 7 | Saving to library | `jobs.JobManager._store()` — moves PDF + cover + sidecar to `LIBRARY_DIR` |

Steps 5–6 are conditional: in `auto` mode (default) they only run when PUA obfuscation is detected; in `always` mode OCR always runs; in `off` mode they are skipped entirely.

### 8.1 Chunked Rendering

For books whose spine has more items than `CHUNK_SIZE`:

1. `extract_spine_idrefs()` reads the ordered spine from the OPF.
2. The spine is split into slices of at most `CHUNK_SIZE` items.
3. For each slice, `_create_chunk_epub()` writes a new ZIP that contains the original manifest (all CSS, images, fonts) but only the spine items for that chunk.
4. The chunk is rendered by `_render_with_retry()`.
5. All chunk PDFs are merged into the final output with `pypdf.PdfWriter`.
6. Temporary chunk EPUBs and PDFs are deleted in the `finally` block.

For small books (spine items ≤ `CHUNK_SIZE`) the chunking overhead is skipped and a single render pass is used.

### 8.2 Retry Logic

`_render_with_retry` retries up to `CHUNK_MAX_RETRIES` times with exponential back-off (`5s`, `10s`, …). The timeout is scaled upward on each attempt (`timeout × attempt_number`). Errors whose message contains any of `"drm"`, `"not a valid"`, `"malformed"`, `"not found"` are treated as non-transient and are not retried.

### 8.3 Adaptive Timeout

Per-chunk timeout = `ADAPTIVE_TIMEOUT_BASE + len(chunk_idrefs) × ADAPTIVE_TIMEOUT_PER_SPINE_ITEM`, with a minimum of `JOB_TIMEOUT_SEC`.

### 8.4 PUA Detection and OCR Text Layer Rebuild

Some commercial CJK ePUBs use a "glyph-shuffling" anti-copy scheme: the text is encoded in Unicode Private Use Area (PUA) codepoints (`U+E000–F8FF`, `U+F0000–FFFFF`, `U+100000–10FFFD`) and the embedded fonts map those PUA slots to the correct glyph shapes. The visual output is pixel-perfect, but the text layer is unreadable — copy/paste, search, screen readers, and translation tools all get PUA gibberish.

This is **not** a bug in the rendering pipeline. The app faithfully preserves what the ePUB contains. There is no formal DRM (`encryption.xml` passes validation), just obfuscated text encoding.

**Detection** (`converter.detect_pua_text`): After rendering, the app extracts text from the first N pages of the PDF using pypdf and computes the fraction of non-ASCII characters that fall in PUA ranges. If this fraction exceeds `PUA_THRESHOLD` (default 20%), the book is considered PUA-obfuscated.

**OCR rebuild** (`converter.add_text_layer`): The app shells out to `ocrmypdf` with the configured `OCR_LANGS`. It first tries `--redo-ocr`, which strips the existing (bogus) text layer and re-OCRs while keeping the crisp vector glyphs intact. If `--redo-ocr` fails (e.g. unsupported page structure), it falls back to `--force-ocr`, which rasterizes pages before OCR (file size grows, but accuracy is maintained).

**Caveats:**
- OCR may introduce occasional character errors compared to the publisher's exact text.
- Vertical-text pages benefit from Tesseract's vertical models (`chi_tra_vert`, `jpn_vert`). Users can add these to `OCR_LANGS` if the models are installed.
- OCR adds significant processing time; the `auto` mode ensures this cost is only paid for obfuscated books.

---

## 9. Job Management

`JobManager` enforces a single active slot: calling `start()` while a job has `status == "running"` raises `RuntimeError`, which `app.py` maps to a `409 Conflict` response.

Job state is purely in memory. A process restart loses in-progress jobs (the UI shows the user no progress) but never corrupts the library on disk. The workdir and upload file are deleted unconditionally in `finally`.

The singleton `manager = JobManager()` is instantiated at module import time and shared across all requests.

---

## 10. Library Management

Books are stored in `LIBRARY_DIR` as a flat directory of file triplets sharing a common stem. The stem is derived from the book title run through `safe_filename()` with a `-epub-to-pdf` suffix appended, and made unique by appending ` (2)`, ` (3)`, … if a collision exists.

All library file access goes through `_safe_member()`, which:
1. Strips any directory component from the supplied filename.
2. Resolves the absolute path and verifies it lies under `LIBRARY_DIR` (preventing path traversal).

`list_books()` reads `.meta.json` sidecars for display titles and cover filenames. If a sidecar is missing or malformed, the PDF stem is used as the title. If a cover file referenced in the sidecar does not exist on disk, the cover is silently omitted.

---

## 11. Data Storage

```
$DATA_DIR/               (default: ./data — override with DATA_DIR env var)
├── tmp/
│   ├── uploads/         Uploaded .epub files (one per pending job; deleted after job ends)
│   └── jobs/
│       └── <job_id>/    Per-job scratch directory (chunk EPUBs, chunk PDFs; deleted after job ends)
└── library/
    ├── <stem>-epub-to-pdf.pdf
    ├── <stem>-epub-to-pdf.cover.jpg    (optional)
    └── <stem>-epub-to-pdf.meta.json
```

- `tmp/` is swept on application startup (removes orphans from crashes).
- `library/` is permanent; files are only removed by explicit user action.
- No relational database is used; all persistence is filesystem-based.

---

## 12. Configuration Reference

All settings are read from environment variables. Copy `env.example` to `.env` and fill in the required values.

### Required

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `ALLOWED_EMAILS` | Comma-separated list of Google emails permitted to sign in |
| `BASE_URL` | Public URL of the deployment, e.g. `https://my-app.zeabur.app` (no trailing slash) |
| `SESSION_SECRET` | Long random string for signing session cookies |
| `DATA_DIR` | Root directory for all persistent data (required in production; mount a persistent volume here) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `CHROMIUM_PATH` | `/usr/bin/chromium` | Path to the Chromium/Chrome binary used by Vivliostyle |
| `REFLOWABLE_PAGE_SIZE` | `A5` | Page size for reflowable books. Vivliostyle presets: `A4`, `A5`, `B5`, `JIS-B5`, `letter`, etc.; custom: `105mm,148mm` |
| `JOB_TIMEOUT_SEC` | `300` | Base per-job render timeout (seconds); also the minimum chunk timeout |
| `MAX_UPLOAD_MB` | `100` | Maximum accepted upload file size (MB) |
| `COVER_THUMB_WIDTH` | `200` | Cover thumbnail width in pixels (aspect ratio preserved) |
| `RECENT_COUNT` | `10` | Number of recently converted books shown on the convert page |
| `CHUNK_SIZE` | `50` | Max spine items per rendering chunk. Set `0` to disable chunking |
| `CHUNK_MAX_RETRIES` | `2` | Retry attempts per failed chunk (with exponential back-off) |
| `ADAPTIVE_TIMEOUT_BASE` | `60` | Fixed part of per-chunk adaptive timeout (seconds) |
| `ADAPTIVE_TIMEOUT_PER_SPINE_ITEM` | `10` | Variable part of per-chunk adaptive timeout (seconds per spine item) |
| `TEXT_LAYER_MODE` | `auto` | When to run OCR: `auto` (only PUA-obfuscated books), `always`, or `off` |
| `OCR_LANGS` | `chi_tra+chi_sim+jpn+kor+eng` | Tesseract language string for OCR |
| `PUA_THRESHOLD` | `0.20` | Fraction of PUA characters to trigger OCR in `auto` mode (0.0–1.0) |

---

## 13. User Interface

The UI is server-rendered HTML (Jinja2) with minimal vanilla JavaScript. Pico CSS v2 (loaded from CDN) provides the base layout.

### Convert Page (`/`)

- **Hero header** — app title, navigation links (Library, Logout), signed-in user avatar and name.
- **Drop zone** — accepts `.epub` via drag-and-drop or file-picker click.
- **Confirm panel** — shows the sanitised filename; user confirms or cancels before rendering begins.
- **Progress panel** — 5-step progress bar with animated spinner on the active step, elapsed-time counter, and ✓/✗ status icons. Polls `/job-status/{id}` every 2 seconds. Reloads the page 2 seconds after the job completes.
- **Recently converted** — card grid showing the last `RECENT_COUNT` books with cover thumbnails and download links.

### Library Page (`/library`)

- Full grid of all converted PDFs sorted newest-first.
- Each card shows: cover thumbnail (or placeholder icon), book title, file size, Download button, Delete button.
- **Delete All** button with a confirmation dialog.

### Login / Error Pages

- `login.html` — Google sign-in button; shown to unauthenticated users.
- `login_error.html` — shown when sign-in fails or the account is not authorised; displays the error message.

---

## 14. Deployment

### Docker (recommended)

The repository includes a `Dockerfile` based on `python:3.12-slim-bookworm` that:

1. Installs system Chromium, Noto CJK fonts, curl, and CA certificates.
2. Installs Node.js 20 from NodeSource.
3. Installs the Vivliostyle CLI globally (`npm install -g @vivliostyle/cli`).
4. Copies and installs Python dependencies.
5. Copies the application source.
6. Exposes port 8000 and starts `uvicorn` with `--workers 1`.

```bash
docker build -t epub-to-pdf .
docker run -p 8000:8000 \
  -v /your/data:/data \
  --env-file .env \
  epub-to-pdf
```

> **Important:** Chromium headless rendering requires adequate `/dev/shm`. On some container runtimes the default (64 MB) is too small for large or fixed-layout books. Add `--shm-size=1g` (or mount a larger `/dev/shm`) if you encounter crashes.

### Zeabur

1. Push to GitHub; create a Zeabur service from the repo (auto-detects `Dockerfile`).
2. Mount a persistent volume at `/data`.
3. Set the environment variables (§12).
4. After first deploy, add the public domain's `/auth` URL as an Authorized redirect URI in Google Cloud Console.

### Local Development

Requirements: Python 3.12+, Node.js 20+, a local Chromium/Chrome binary.

```bash
pip install -r requirements.txt
npm install -g @vivliostyle/cli
export $(grep -v '^#' .env | xargs)
uvicorn app:app --reload --port 8000
```

---

## 15. Security Considerations

| Area | Measure |
|---|---|
| Authentication | Google OAuth 2.0 + OpenID Connect; no passwords handled by the app |
| Authorisation | Email allowlist; fails closed (empty list → deny all) |
| Session integrity | HMAC-signed cookies (`itsdangerous`); `https_only=True` on HTTPS deployments |
| File path traversal | `_safe_member()` in `library.py` strips directory components and verifies the resolved path is under `LIBRARY_DIR` |
| Upload validation | Extension check (`.epub` only); size limit enforced during streaming |
| DRM detection | `encryption.xml` is parsed; any non-font-obfuscation algorithm causes rejection |
| Filename sanitisation | `safe_filename()` strips all characters outside `[\w.\- ]` (Unicode-aware), caps length at 180 |
| Sandbox | Vivliostyle/Chromium runs with its sandbox disabled (standard practice for headless rendering in containers). Acceptable for a private single-user deployment; for hardened environments, run the container as a non-root user and/or enable the `--no-sandbox` flag explicitly |
| Secrets | `SESSION_SECRET` must be a long random value in production; the default `"dev-insecure-change-me"` is intentionally insecure |

---

## 16. Known Limitations

| Limitation | Detail |
|---|---|
| Single concurrent conversion | `JobManager` enforces one conversion at a time. A second request returns `409 Conflict` |
| In-memory job state | A process restart loses any in-flight job. The library on disk is never affected |
| Single-user | No per-user isolation; all signed-in users share the same library (designed for use by one person or a small, trusted household) |
| No background queue | Uploads while a job is running are rejected; the user must wait and retry |
| Vivliostyle version drift | The `Dockerfile` installs the latest `@vivliostyle/cli` at build time. Pin a specific version (e.g. `@vivliostyle/cli@9.x`) for reproducible builds |
| `/dev/shm` constraint | Chromium uses shared memory; the default container limit can cause crashes on large or fixed-layout books |
| Vertical text requires source CSS | Vivliostyle honours `writing-mode` from the ePUB's own CSS. If the source book does not declare `vertical-rl`, the output will be horizontal |
| No test suite in repo | The module separation (especially `build_vivliostyle_cmd` as a pure function) is designed for testability, but no automated tests are currently present |
| PUA-obfuscated text | Some commercial CJK ePUBs use PUA codepoints as an anti-copy measure. The app auto-detects this and rebuilds the text layer via OCR (`TEXT_LAYER_MODE=auto`). OCR may introduce occasional character errors vs. the publisher's exact text. Vertical text benefits from Tesseract vertical models (`chi_tra_vert`, `jpn_vert`) |
| OCR processing time | OCR (via ocrmypdf + Tesseract) adds significant time to conversion. In `auto` mode this cost is only paid for PUA-obfuscated books; clean books skip OCR entirely |
