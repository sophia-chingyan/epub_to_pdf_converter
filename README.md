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
- **PUA text layer detection and OCR rebuild** — some CJK ePUBs use Private
  Use Area font obfuscation that renders perfectly but produces unselectable /
  unsearchable text. The app auto-detects this and rebuilds the text layer via
  OCR (ocrmypdf + Tesseract), so copy/paste, search, and screen readers work.
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
              ├─ Library (library.py)           PDFs + covers on disk
              └─ /healthz                       platform health check
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

Copy `env.example` to `.env` and fill it in. Key values:

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ✅ | From step 1 |
| `ALLOWED_EMAILS` | ✅ | Comma-separated; your email |
| `BASE_URL` | ⚠️ | e.g. `https://your-app.up.railway.app` (no trailing slash). Optional on Railway — derived from `RAILWAY_PUBLIC_DOMAIN`; required for a custom domain or another host |
| `SESSION_SECRET` | ✅ | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATA_DIR` | ⚠️ | Must point at a persistent volume in production. Defaults to `/data` in the image, or to `RAILWAY_VOLUME_MOUNT_PATH` if you clear it |
| `REFLOWABLE_PAGE_SIZE` | optional | Default `A5`. Vivliostyle presets: `A4`, `A5`, `B5`, `JIS-B5`, `letter`, etc., or a custom size like `105mm,148mm` |
| `JOB_TIMEOUT_SEC` | optional | Default `300`. Base per-job timeout; adaptive chunk timeouts may exceed this |
| `MAX_UPLOAD_MB` | optional | Default `100` |
| `COVER_THUMB_WIDTH` | optional | Default `200` (pixels). Cover images are downscaled to this width |
| `RECENT_COUNT` | optional | Default `10`. Number of recent books shown on the convert page |
| `CHUNK_SIZE` | optional | Default `50`. Max spine items per rendering chunk. Set `0` to disable chunking |
| `CHUNK_MAX_RETRIES` | optional | Default `2`. Retry attempts per failed chunk (with exponential back-off) |
| `ADAPTIVE_TIMEOUT_BASE` | optional | Default `60` (seconds). Fixed part of the per-chunk adaptive timeout |
| `ADAPTIVE_TIMEOUT_PER_SPINE_ITEM` | optional | Default `10` (seconds per spine item). Variable part of the per-chunk adaptive timeout |
| `TEXT_LAYER_MODE` | optional | Default `auto`. When to run OCR: `auto` (PUA-obfuscated books only), `always`, or `off` |
| `OCR_LANGS` | optional | Default `chi_tra+chi_sim+jpn+kor+eng`. Tesseract languages for OCR. The vertical models are installed and added automatically for vertical-text books |
| `OCR_JOBS` | optional | Default: CPU count capped at `4`. Parallel OCR workers. Lower it to `1` on a memory-constrained instance |
| `PUA_THRESHOLD` | optional | Default `0.20`. Fraction of PUA chars to trigger OCR in auto mode |
| `CHROMIUM_PATH` | optional | Browser binary used by Vivliostyle. The image sets it to `/usr/local/bin/chromium-container`, a wrapper that adds the container-safe Chromium flags; point it at a real browser only for local development |

---

## 3. Local development

Requires Python 3.12+, Node.js 22.12+, and a Chromium/Chrome binary.

```bash
pip install -r requirements.txt
npm install -g @vivliostyle/cli@11.3.3   # same pin as the Dockerfile

# point CHROMIUM_PATH at your local browser, e.g. on macOS:
#   export CHROMIUM_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export $(grep -v '^#' .env | xargs)   # load .env
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000.

---

## 4. Deploy on Railway

This repo ships a `Dockerfile` (Python, Node 22, system Chromium, Noto CJK
fonts, Tesseract) and a `railway.json` that tells Railway to build it, health
check `/healthz`, and run exactly one replica.

1. Push the repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** and pick this repo.
   Railway reads `railway.json`, so the Dockerfile builder and health check are
   configured for you. The first build takes a while — it installs Chromium,
   Node, and the CJK OCR models.
3. **Settings → Networking → Generate Domain**. This both gives you a URL and
   sets `RAILWAY_PUBLIC_DOMAIN`, which the app uses as `BASE_URL` — so you do
   not have to know the domain in advance.
4. **Attach a volume**: *service → Data / Volumes → Add Volume*, mount path
   `/data`. **Do this before you convert anything.** Without a volume the
   library lives on the container filesystem and every redeploy wipes it.
5. **Variables** — set these (see section 2 for the full list):

   | Variable | Value |
   |---|---|
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 1 |
   | `ALLOWED_EMAILS` | your Google address |
   | `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |

   `BASE_URL` and `DATA_DIR` are left unset on purpose: the generated domain and
   the `/data` mount are picked up automatically. Set `BASE_URL` explicitly only
   when you add a custom domain.
6. In Google Cloud, add `https://YOUR-DOMAIN/auth` as an Authorized redirect URI
   (section 1, step 4), then redeploy or just sign in.

Railway sets `PORT` and terminates TLS at its edge; the container binds
`0.0.0.0:$PORT` and runs uvicorn with `--proxy-headers`, so both are handled.

**Check the deploy logs after the first boot.** The app logs its effective
`BASE_URL` and `DATA_DIR`, and prints a `CONFIG:` warning for each thing that
will bite you later — a default `SESSION_SECRET`, missing OAuth credentials, an
empty allowlist, or a library directory that is not on the volume. The app still
boots and serves its login page in that state, so a healthy deploy is not by
itself proof that it is configured.

### Sizing and cost

Conversions are CPU- and memory-hungry (headless Chromium, plus Tesseract when a
book needs OCR). A book that renders fine on a laptop can OOM on a small
instance, which shows up as a conversion that fails with a Chromium error rather
than as a crash. If that happens, give the service more memory, or lower
`CHUNK_SIZE` (say `20`) and `OCR_JOBS` (say `1`) to shrink the peak.

Because conversions are serialised in memory and the volume attaches to a single
instance, keep `numReplicas` at 1. Leave `sleepApplication` off as well: a
sleeping instance would be suspended mid-conversion.

### Other platforms

Any host that builds a Dockerfile, injects `PORT`, and can mount a volume at
`/data` works the same way — Zeabur, Fly.io, Render, or plain `docker run`. Only
`BASE_URL` needs setting by hand there, since `RAILWAY_PUBLIC_DOMAIN` is
Railway-specific.

```bash
docker build -t epub2pdf .
docker run --rm -p 8000:8000 --env-file .env -v epub-data:/data epub2pdf
```

---

## 5. Troubleshooting

**Sign-in fails / redirect_uri_mismatch.**
`BASE_URL` + `/auth` must exactly equal the Authorized redirect URI in Google
Cloud, including `https://` and no trailing slash.

**"Access Denied" after signing in.**
Your email isn't in `ALLOWED_EMAILS`, or the consent screen is in Testing mode
and you haven't added yourself as a test user.

**Rendering fails or hangs on large books (Chromium / `/dev/shm`).**
Headless Chromium puts large buffers in `/dev/shm`, which is 64 MB in most
container runtimes and cannot be resized on Railway. The image therefore
launches Chromium through `scripts/chromium-container.sh`, which passes
`--disable-dev-shm-usage` so those buffers go to `/tmp` instead. If a big or
fixed-layout book still fails, raise `JOB_TIMEOUT_SEC` and/or lower `CHUNK_SIZE`
(e.g. `20`) so each Chromium invocation does less work. Note that this only
applies when `CHROMIUM_PATH` points at that wrapper — if you override it with a
raw browser path, you lose the flag.

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
The Dockerfile pins `@vivliostyle/cli` via the `VIVLIOSTYLE_VERSION` build
argument (and Node via `NODE_MAJOR`), so every rebuild renders like the deploy
you tested. Previously the image installed whatever was latest at build time,
which meant a redeploy months later could silently cross a major version — and
because npm resolves the newest release the image's Node supports, the version
you got also drifted with the base image's Node minor. Bump the two pins
together: Vivliostyle 11.0.1+ requires Node ≥ 22.12. On Railway you can override
either as a service variable instead of editing the Dockerfile.

**The deploy is stuck on "waiting for health check".**
Railway polls `/healthz`, which answers as soon as the web process is up and
never touches disk or a subprocess. If it never turns green the app failed to
start — read the deploy logs for the traceback. A conversion running in the
background does not affect it.

**The library is empty after a redeploy.**
No volume was attached, so `/data` was part of the container filesystem. Attach
one at `/data` (section 4, step 4); converted PDFs from before that are gone.
The startup log warns about this on every boot.

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
  norm for headless rendering in containers); the image's launcher wrapper
  passes `--no-sandbox` explicitly so this does not depend on that default.
  Acceptable for a private, single-user tool that only renders books you upload
  yourself; if you prefer, run the container as a non-root user.
- Vivliostyle is AGPLv3. Running it as a private single-user tool does not
  trigger the network-distribution clause.
- **PUA-obfuscated text**: Some commercial CJK ePUBs encode text in Unicode
  Private Use Area codepoints (a soft anti-copy measure). The PDF looks perfect,
  but copy/paste/search returns gibberish. In `auto` mode (default) the app
  detects this and rebuilds the text layer via OCR. A small "text layer rebuilt
  via OCR" note is stored in the book's metadata. Caveats:
  - OCR may introduce occasional character errors vs. the publisher's exact text.
  - Vertical-text pages benefit from Tesseract vertical models (`chi_tra_vert`,
    `jpn_vert`); the image installs them and the app adds them automatically for
    vertical books.
  - OCR adds processing time; the `auto` mode only pays this cost for obfuscated
    books.
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
