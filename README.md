# ePUB → PDF Converter

A private, single-user web app that converts ePUB files to PDF with high
fidelity — preserving images, links, paragraph structure, and the table of
contents (as PDF bookmarks) — with first-class support for **vertical and
horizontal CJK typesetting** (Traditional/Simplified Chinese, Japanese with
furigana/ruby, and Korean), as well as English.

Rendering is done by the [Vivliostyle CLI](https://vivliostyle.org/), which is
purpose-built for paged, vertical-writing-mode CJK output. The web layer is
Python (FastAPI + Jinja2) with app-level Google sign-in locked to an email
allowlist.

---

## Features

- **Convert page** — drag-and-drop an `.epub`, watch a live 5-step progress
  view, and see recently converted PDFs.
- **Library page** — all your converted PDFs with cover thumbnails; download,
  delete one, bulk-delete, or delete all.
- **Reflowable and fixed-layout** ePUBs.
- **Vertical text** (`writing-mode: vertical-rl`) and right-to-left reading
  progression handled correctly.
- **Chunked rendering** — large books are automatically split into spine-item
  chunks, each rendered separately and merged with pypdf, so big or complex
  books that would time out or crash Chromium in one pass succeed reliably.
  Each chunk is also retried with exponential back-off on transient errors.
- **Bundled Noto CJK fonts** as a fallback; fonts embedded in the ePUB are
  honoured first.
- **DRM-protected ePUBs are detected and rejected** (this tool does not strip
  DRM).
- **Google sign-in** restricted to your own account.

---

## Architecture

```
Browser ──► FastAPI (app.py)
              ├─ Google OAuth (auth.py)         single-email allowlist
              ├─ Jinja2 templates               convert / library / login pages
              ├─ Job manager (jobs.py)          one conversion at a time
              │     └─ converter.py             validate → cover → Vivliostyle
              │            └─ `vivliostyle build …`  (Node CLI → Chromium)
              └─ Library (library.py)           PDFs + covers on disk
```

Storage layout under `DATA_DIR`:

```
/data/
  tmp/uploads/   uploaded .epub files (deleted after each job)
  tmp/jobs/      per-job scratch space (deleted after each job)
  library/       <stem>.pdf, <stem>.cover.<ext>, <stem>.meta.json  (permanent)
```

PDFs are kept permanently until you delete them from the Library page. Only the
temporary upload/scratch files are auto-cleaned (also swept on startup).

---

## 1. Google OAuth setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) →
   **APIs & Services → Credentials**.
2. Configure the **OAuth consent screen** (External is fine; you can leave it in
   "Testing" and add your own email as a test user).
3. **Create Credentials → OAuth client ID → Web application**.
4. Under **Authorized redirect URIs**, add exactly:
   ```
   https://YOUR-DOMAIN/auth
   ```
   (and `http://localhost:8000/auth` if you test locally).
5. Copy the **Client ID** and **Client secret** into your environment
   (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).
6. Put your Google email in `ALLOWED_EMAILS`.

> The redirect URI must match `BASE_URL` + `/auth` exactly, or Google will
> reject the sign-in.

---

## 2. Environment variables

Copy `.env.example` to `.env` and fill it in. Key values:

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ✅ | From step 1 |
| `ALLOWED_EMAILS` | ✅ | Comma-separated; your email |
| `BASE_URL` | ✅ | e.g. `https://your-app.zeabur.app` (no trailing slash) |
| `SESSION_SECRET` | ✅ | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATA_DIR` | ✅ (prod) | Point at a persistent volume |
| `REFLOWABLE_PAGE_SIZE` | optional | Default `A5`. Vivliostyle presets: `A4`, `A5`, `B5`, `JIS-B5`, `letter`, etc., or a custom size like `105mm,148mm` |
| `JOB_TIMEOUT_SEC` | optional | Default `300`. Base per-job timeout; adaptive chunk timeouts may exceed this |
| `MAX_UPLOAD_MB` | optional | Default `100` |
| `COVER_THUMB_WIDTH` | optional | Default `200` (pixels). Cover images are downscaled to this width |
| `RECENT_COUNT` | optional | Default `10`. Number of recent books shown on the convert page |
| `CHUNK_SIZE` | optional | Default `50`. Max spine items per rendering chunk. Set `0` to disable chunking |
| `CHUNK_MAX_RETRIES` | optional | Default `2`. Retry attempts per failed chunk (with exponential back-off) |
| `ADAPTIVE_TIMEOUT_BASE` | optional | Default `60` (seconds). Fixed part of the per-chunk adaptive timeout |
| `ADAPTIVE_TIMEOUT_PER_SPINE_ITEM` | optional | Default `10` (seconds per spine item). Variable part of the per-chunk adaptive timeout |
| `CHROMIUM_PATH` | optional | Default `/usr/bin/chromium`. Path to the Chromium/Chrome binary used by Vivliostyle |

---

## 3. Local development

Requires Python 3.12+, Node.js 20+, and a Chromium/Chrome binary.

```bash
pip install -r requirements.txt
npm install -g @vivliostyle/cli

# point CHROMIUM_PATH at your local browser, e.g. on macOS:
#   export CHROMIUM_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export $(grep -v '^#' .env | xargs)   # load .env
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000.

---

## 4. Deploy on Zeabur

This repo ships a `Dockerfile` that installs Python, Node 20, system Chromium,
and the Noto CJK fonts.

1. Push the repo to GitHub (or use Zeabur's Git deploy).
2. In Zeabur, create a service from the repo — it will detect the `Dockerfile`.
3. Add a **persistent volume** mounted at `/data` so your library survives
   restarts and redeploys.
4. Set the environment variables from section 2 (especially `BASE_URL` =
   your Zeabur public domain, and the OAuth values).
5. Deploy. Once it's up, register that public domain's `/auth` URL as an
   Authorized redirect URI in Google Cloud (section 1, step 4).

The container listens on `$PORT` (default 8000); Zeabur sets this automatically.

---

## 5. Troubleshooting

**Sign-in fails / redirect_uri_mismatch.**
`BASE_URL` + `/auth` must exactly equal the Authorized redirect URI in Google
Cloud, including `https://` and no trailing slash.

**"Access Denied" after signing in.**
Your email isn't in `ALLOWED_EMAILS`, or the consent screen is in Testing mode
and you haven't added yourself as a test user.

**Rendering fails or hangs on large books (Chromium / `/dev/shm`).**
Headless Chromium uses shared memory (`/dev/shm`), which defaults to a small
size in containers and can cause crashes on big/fixed-layout books. If you hit
this, increase the container's shared memory (e.g. a larger `shm-size`, or a
`/dev/shm` mount with more space) and/or raise `JOB_TIMEOUT_SEC`. Chunked
rendering (controlled by `CHUNK_SIZE`) already helps here; reducing `CHUNK_SIZE`
further (e.g. `20`) gives each chunk less work to do per Chromium invocation.

**A chunk fails and the whole job aborts.**
The app retries each chunk up to `CHUNK_MAX_RETRIES` times with exponential
back-off. If retries are exhausted, try reducing `CHUNK_SIZE` so chunks are
smaller, or increase `ADAPTIVE_TIMEOUT_BASE` / `ADAPTIVE_TIMEOUT_PER_SPINE_ITEM`
to give each chunk more time.

**Chinese/Japanese/Korean glyphs missing or boxes (tofu).**
The image bundles `fonts-noto-cjk` + `fonts-noto-cjk-extra`. If a book embeds
its own fonts they're used first. If you still see tofu, confirm the font
packages installed during the image build.

**Vivliostyle version issues.**
The Dockerfile installs the latest `@vivliostyle/cli` at build time. For
reproducible builds, pin a version (e.g. `@vivliostyle/cli@9.x`) once you've
confirmed it renders your books. Requires Node ≥ 20.

**Wrong page size for novels.**
Reflowable books use `REFLOWABLE_PAGE_SIZE` (default `A5`). For a pocket-novel
feel try `JIS-B6` or a custom `105mm,148mm`. Fixed-layout books ignore this and
keep their own geometry.

**Vertical text isn't vertical.**
Vivliostyle honours the ePUB's own `writing-mode` and the spine's
`page-progression-direction`. If a book looks horizontal, its source CSS
probably doesn't set `vertical-rl`.

---

## Notes & limitations

- One conversion runs at a time; a second request returns "already running".
- Job progress is in memory — a restart mid-conversion loses that job (never the
  library).
- Chromium runs with its sandbox disabled (the Vivliostyle CLI default, and the
  norm for headless rendering in containers). Acceptable for a private,
  single-user tool; if you prefer, run the container as a non-root user.
- Vivliostyle is AGPLv3. Running it as a private single-user tool does not
  trigger the network-distribution clause.
- **Chunked rendering limitations**: When a book is large enough to be split into
  chunks, the following fidelity trade-offs apply:
  - **PDF bookmarks (TOC)**: Each chunk's TOC bookmarks only cover chapters
    within that chunk. The merged PDF may have a fragmented or mis-targeted
    outline.
  - **Cross-chapter hyperlinks**: Internal links that reference a chapter in a
    different chunk cannot be resolved and will be broken in the final PDF.
  - **Page numbering / running heads**: These reset at each chunk boundary
    because each chunk is rendered as an independent document.
  
  These are inherent to the split-and-merge approach. For books where TOC
  bookmarks and cross-chapter links are critical, set `CHUNK_SIZE=0` to disable
  chunking (at the risk of longer render times or timeouts for very large books).
