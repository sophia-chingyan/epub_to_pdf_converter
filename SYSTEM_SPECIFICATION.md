# System Specification — ePUB ⇄ PDF Converter

**Version:** 1.1  
**Last updated:** 2026-09-18  
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

This application is a **private, single-user web service** that converts ePUB e-books into PDF documents, and PDF documents into reflowable ePUB e-books, with high fidelity. It is designed for personal use and is restricted to a configurable email allowlist via Google OAuth.

### Primary Goals

- Convert reflowable and fixed-layout ePUBs to PDF.
- Convert PDFs (born-digital, and scanned or PUA-obfuscated ones via OCR) to reflowable EPUB 3.
- Preserve document structure in both directions: images, hyperlinks, paragraph styles, table of contents (PDF bookmarks ⇄ ePUB navigation), and vertical/horizontal CJK typesetting (including ruby/furigana).
- Provide first-class support for **CJK scripts** (Traditional/Simplified Chinese, Japanese with ruby/furigana, Korean) and English.
- Handle very large books reliably through chunked, retried rendering.
- Offer a simple browser-based UI with drag-and-drop upload, live progress display, and a persistent library of converted PDFs.

### Out of Scope

- Multi-user tenancy or per-user libraries.
- Stripping or circumventing DRM (DRM-protected files are detected and rejected).
- Conversion from formats other than ePUB and PDF.
- Reproducing a PDF's exact page layout in the ePUB (the ePUB is reflowable by design; fonts are not embedded).

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
| `pymupdf>=1.24` | PDF → ePUB: text/glyph geometry, images, links, bookmarks, tables, page rendering |

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

### `pdf2epub.py` — PDF inspection and ePUB building

The PDF → ePUB counterpart of `converter.py`, built on PyMuPDF (no subprocesses except the optional OCR pre-pass, which reuses `converter.add_text_layer` / ocrmypdf).

Responsibilities:
- **Validation** (`validate`) — `%PDF` header, openable, not password-protected, at least one page.
- **Metadata & cover** (`extract_info`, `cover_image`) — title/author from the document info (file-name-like titles are discarded), language from the catalog `/Lang`; the cover is page 1's own full-page image when it has one, else a render of page 1.
- **Analysis pass** (`analyze`) — one cheap pass over all pages: writing mode (vertical vs. horizontal, by glyph geometry), script statistics → language guess (`ScriptStats`), body font size (character-weighted mode), heading size ladder, running header/footer signatures (repeated text, or a repeated slot with small/short text), text coverage (scanned detection) and PUA fraction.
- **OCR pre-pass** (`prepare_text_layer`) — with `PDF_OCR_MODE=auto`: `ocrmypdf --skip-text` for scanned PDFs, `--force-ocr` (via `converter.add_text_layer`) for PUA-obfuscated ones; the upload is modified in place and re-analysed.
- **Page → units** (`page_units`) — glyphs are grouped into *runs* (a horizontal line, or a vertical column — both `WMode 1` fonts and Chromium-style stacked one-glyph lines are recognised), lines MuPDF split at wide justified gaps are re-joined, page-wide column gutters are detected and lines split there, ruby runs (small kana/bopomofo beside a larger run) are folded into their base glyphs, link annotations tag glyphs (with a nearest-column fallback for Chromium's misplaced vertical-text link rectangles), ruled tables become `<table>` units, vector-drawing clusters without text are rasterised, images are extracted by xref (soft masks folded into PNG; identical images stored once), running heads/page numbers are dropped, and units are ordered by a recursive XY-cut that prefers the widest empty stripe (so column gutters beat inter-line gaps and full-width headings force a top/bottom split first).
- **Units → paragraphs** (`Builder`) — headings from font size (or the bookmark level when the unit is a bookmark target), paragraph continuation across units and pages (indent / full-line / spacing / CJK dialogue-opener cues), alignment classes, `<small>` text, chapter files split at level-1 bookmarks (or h1 without bookmarks) and at a soft size cap; page anchors and bookmark anchors are recorded for link and navigation resolution.
- **Packaging** (`write_epub`) — EPUB 3 zip: stored `mimetype` first, `container.xml`, OPF (language, `dcterms:modified`, cover, `primary-writing-mode`, `page-progression-direction="rtl"` for vertical books), `nav.xhtml` (nested from bookmarks, else from headings), EPUB 2 `toc.ncx`, `styles.css` (vertical-rl rules when needed, indent policy voted from the document), cover page, chapter XHTML files, images.

### `jobs.py` — Job management

Implements a single-slot, in-memory job queue (`JobManager`) backed by a `threading.Lock` and a daemon thread. The pipeline is chosen by the upload's extension (`.epub` → `_run_epub_to_pdf`, `.pdf` → `_run_pdf_to_epub`); both report five steps.

`Job` dataclass fields: `id`, `display_name`, `status` (`running`|`done`|`error`), `current_step`, `current_label`, `steps` (completed step log), `error`, `output_name`, `direction` (`epub-to-pdf`|`pdf-to-epub`).

`JobManager` exposes:
- `start(upload_path, display_name)` — creates a job, starts the worker thread, returns `job_id`.
- `get(job_id)` — thread-safe job lookup.
- `is_busy()` — returns `True` if a job with `status == "running"` exists.

After a successful conversion the output file, cover thumbnail, and `.meta.json` sidecar are moved from the job workdir into the library. The workdir and the upload file are always cleaned up in the `finally` block.

### `library.py` — Book library

Reads and writes the permanent library directory. Each book is represented by three files with a shared stem:

| File | Contents |
|---|---|
| `<stem>-epub-to-pdf.pdf` or `<stem>-pdf-to-epub.epub` | Converted book |
| `<stem>-….cover.<ext>` | Cover thumbnail (optional) |
| `<stem>-….meta.json` | JSON sidecar: `title`, `file`, `format` (`PDF`/`EPUB`), `cover`, optional `ocr_note`; plus `fixed_layout` (+ legacy `pdf`) for PDFs, `language`, `vertical`, `pages` for ePUBs |

Key functions:
- `list_books(limit)` — returns books (PDF and ePUB) sorted newest-first; reads sidecar for title, cover filename and note.
- `book_path(name)` / `pdf_path(name)` / `cover_path(name)` — resolve a filename to a safe absolute path, refusing directory traversal.
- `media_type(path)` — `application/pdf` or `application/epub+zip` for downloads.
- `delete_book(name)` — deletes the book, sidecar, and all cover files matching the stem.
- `delete_all()` — iterates all books and calls `delete_book`.

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
| `GET` | `/healthz` | — | Liveness probe for the platform health check; no disk or subprocess work |
| `GET` | `/` | ✅ | Convert page (or login page if signed out) |
| `GET` | `/library` | ✅ | Library page |
| `GET` | `/login` | — | Redirect to Google OAuth authorisation URL |
| `GET` | `/auth` | — | OAuth callback; validates token, checks allowlist, sets session |
| `GET` | `/logout` | — | Clears session, redirects to `/` |
| `POST` | `/upload` | ✅ | Stream-upload an `.epub` or `.pdf`; returns `{"filename": "<safe_name>"}` |
| `POST` | `/start-convert/{filename}` | ✅ | Start a conversion job (direction from the extension); returns `{"job_id": "<hex>"}` |
| `GET` | `/job-status/{job_id}` | ✅ | Poll job progress; returns `Job.to_dict()` |
| `GET` | `/download/{name}` | ✅ | Download a converted PDF or ePUB (media type by extension) |
| `GET` | `/cover/{name}` | ✅ | Serve a cover thumbnail |
| `POST` | `/delete/{name}` | ✅ | Delete one book (PDF + sidecar + cover) |
| `POST` | `/delete-all` | ✅ | Delete all books; returns list of deleted filenames |

### Upload constraints

- Accepts only `.epub` and `.pdf` files (checked by extension on the server); the extension selects the pipeline.
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
  "output_name": null,
  "direction": "epub-to-pdf | pdf-to-epub"
}
```

When `status` is `"done"`, `output_name` holds the output filename (PDF or ePUB) for use with `/download/{name}`.

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

### 8.0 ePUB → PDF

Each `.epub` job runs the following pipeline in a background daemon thread:

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

### 8.0b PDF → ePUB

Each `.pdf` job runs this pipeline (same five UI steps):

| Step | Label | Action |
|---|---|---|
| 1 | Validating PDF | `pdf2epub.validate()` — `%PDF` header, openable, not password-protected |
| 2 | Extracting metadata & cover | `pdf2epub.extract_info()` — title, author, `/Lang`, page count, cover thumbnail |
| 3 | Analysing layout | `pdf2epub.analyze()` — writing mode, language, body size, heading ladder, running heads; then `prepare_text_layer()` runs OCR for scanned / PUA-obfuscated PDFs when `PDF_OCR_MODE=auto` (progress shown in the same step) |
| 4 | Building ePUB | `pdf2epub.convert()` — page-by-page unit extraction, paragraph assembly, EPUB packaging; progress reports the page number |
| 5 | Saving to library | `jobs.JobManager._store()` — moves ePUB + cover + sidecar to `LIBRARY_DIR` |

Vertical text is decided per document by majority of glyphs: a vertical book gets `writing-mode: vertical-rl`, `page-progression-direction="rtl"` and the `primary-writing-mode` meta; its columns are re-joined into paragraphs in right-to-left order. Headers, footers and page numbers are removed by repetition across pages (text signature with digits collapsed, or a repeated slot with small/short text). The language is the catalog `/Lang` refined by script statistics (`zh` → `zh-TW`/`zh-CN`; an implausible `en` on a CJK book is overridden).

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

Books are stored in `LIBRARY_DIR` as a flat directory of file triplets sharing a common stem. The stem is derived from the book title run through `safe_filename()` with a `-epub-to-pdf` (PDF output) or `-pdf-to-epub` (ePUB output) suffix appended, and made unique by appending ` (2)`, ` (3)`, … if a collision exists.

All library file access goes through `_safe_member()`, which:
1. Strips any directory component from the supplied filename.
2. Resolves the absolute path and verifies it lies under `LIBRARY_DIR` (preventing path traversal).

`list_books()` reads `.meta.json` sidecars for display titles and cover filenames. If a sidecar is missing or malformed, the PDF stem is used as the title. If a cover file referenced in the sidecar does not exist on disk, the cover is silently omitted.

---

## 11. Data Storage

```
$DATA_DIR/               (default: ./data — override with DATA_DIR env var)
├── tmp/
│   ├── uploads/         Uploaded .epub / .pdf files (one per pending job; deleted after job ends)
│   └── jobs/
│       └── <job_id>/    Per-job scratch directory (chunk EPUBs, chunk PDFs; deleted after job ends)
└── library/
    ├── <stem>-epub-to-pdf.pdf
    ├── <stem>-epub-to-pdf.cover.jpg    (optional)
    ├── <stem>-epub-to-pdf.meta.json
    ├── <stem>-pdf-to-epub.epub
    ├── <stem>-pdf-to-epub.cover.jpg    (optional)
    └── <stem>-pdf-to-epub.meta.json
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
| `BASE_URL` | Public URL of the deployment, e.g. `https://my-app.up.railway.app` (no trailing slash). Falls back to `RAILWAY_PUBLIC_DOMAIN` when unset |
| `SESSION_SECRET` | Long random string for signing session cookies |
| `DATA_DIR` | Root directory for all persistent data (required in production; mount a persistent volume here). Falls back to `RAILWAY_VOLUME_MOUNT_PATH` when unset |

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
| `PDF_OCR_MODE` | `auto` | PDF → ePUB: `auto` repairs scanned / PUA-obfuscated PDFs with OCR before extraction; `off` converts the existing text layer as is |
| `PDF_TABLES` | `1` | PDF → ePUB: detect ruled tables and emit `<table>` (horizontal text only) |
| `PDF_DRAWINGS` | `1` | PDF → ePUB: rasterise vector drawings (charts, diagrams) as PNG figures |

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

> **Important:** Chromium headless rendering needs more shared memory than the 64 MB `/dev/shm` most container runtimes give it, or large and fixed-layout books crash the renderer. The image therefore points `CHROMIUM_PATH` at `scripts/chromium-container.sh`, which passes `--disable-dev-shm-usage` (and `--no-sandbox`) on every launch — Vivliostyle only applies that flag by itself inside its own official image. Where the runtime allows it, `--shm-size=1g` is still worth adding; on Railway it is not configurable, which is why the flag is in the wrapper.

### Railway

`railway.json` declares the Dockerfile builder, the `/healthz` health check, a
single replica, and restart-on-failure.

1. Push to GitHub; create a Railway service from the repo.
2. Generate a domain (Settings → Networking). This sets `RAILWAY_PUBLIC_DOMAIN`,
   which the app uses as `BASE_URL`.
3. Attach a volume mounted at `/data`, before converting anything — without one
   the library is wiped by every redeploy.
4. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ALLOWED_EMAILS`, and
   `SESSION_SECRET` (§12). `BASE_URL` and `DATA_DIR` derive themselves.
5. Add the public domain's `/auth` URL as an Authorized redirect URI in Google
   Cloud Console.

The container binds `0.0.0.0:$PORT` and runs uvicorn with `--proxy-headers`, so
the platform's injected port and TLS-terminating edge are both handled. Startup
logs the effective `BASE_URL`/`DATA_DIR` and a `CONFIG:` warning per
misconfiguration (see §12); the app still boots in that state, so a passing
health check is not by itself evidence that it is configured.

Single replica is a requirement, not a default: conversions are serialised
through in-memory job state and the volume attaches to one instance.

### Other platforms

Any host that builds a `Dockerfile`, injects `PORT`, and can mount a volume at
`/data` works the same way (Zeabur, Fly.io, Render, plain `docker run`). Only
`BASE_URL` must be set by hand, since `RAILWAY_PUBLIC_DOMAIN` is Railway-specific.

### Local Development

Requirements: Python 3.12+, Node.js 22.12+, a local Chromium/Chrome binary.

```bash
pip install -r requirements.txt
npm install -g @vivliostyle/cli@11.3.3   # same pin as the Dockerfile
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
| Upload validation | Extension check (`.epub` / `.pdf` only); size limit enforced during streaming |
| DRM detection | ePUB: `encryption.xml` is parsed; any non-font-obfuscation algorithm causes rejection. PDF: password-protected files are rejected |
| PDF parsing | PyMuPDF runs in-process; a page that fails to parse is skipped with a log line rather than failing the job |
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
| Test coverage | `tests/` covers the PDF → ePUB pipeline (Chromium-rendered fixtures in English, vertical Japanese with ruby, Traditional Chinese with bopomofo, Korean; scanned and bookmark-less PDFs) and the upload → convert → download flow. The ePUB → PDF render path (Vivliostyle) has no automated tests |
| PDF → ePUB is heuristic | Paragraphs, headings, columns, ruby and running heads are inferred from glyph geometry. Unusual layouts (text over images, multi-band vertical layouts with sidebars, footnotes) may come out in the wrong order or as plain paragraphs; the ePUB is reflowable, so the PDF's exact pagination is not reproduced |
| PDF → ePUB fonts | Fonts are not embedded; the reading system's fonts (and its CJK fallbacks) are used |
| PUA-obfuscated text | Some commercial CJK ePUBs use PUA codepoints as an anti-copy measure. The app auto-detects this and rebuilds the text layer via OCR (`TEXT_LAYER_MODE=auto`). OCR may introduce occasional character errors vs. the publisher's exact text. Vertical text benefits from Tesseract vertical models (`chi_tra_vert`, `jpn_vert`) |
| OCR processing time | OCR (via ocrmypdf + Tesseract) adds significant time to conversion. In `auto` mode this cost is only paid for PUA-obfuscated books; clean books skip OCR entirely |
