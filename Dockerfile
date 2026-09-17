# ePUB -> PDF converter
# Bundles: Python app + Node 22 (for Vivliostyle CLI) + system Chromium + Noto CJK fonts.

FROM python:3.12-slim-bookworm

# Pinned so a rebuild renders identically to the deploy you tested. Installing
# whatever is latest at build time lets a redeploy months later silently cross a
# major version; worse, npm resolves the newest release the image's Node
# supports, so the version drifted with the base image too. Vivliostyle raises
# its Node floor across majors, so bump this and NODE_MAJOR together.
ARG VIVLIOSTYLE_VERSION=11.3.3
ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CHROMIUM_PATH=/usr/local/bin/chromium-container \
    DATA_DIR=/data \
    PORT=8000

# System packages: Chromium (pulls its own runtime libs), Noto CJK fonts,
# OCR tooling (ocrmypdf + Tesseract with CJK language packs), Ghostscript,
# and the tools needed to add the NodeSource repo.
#
# NOTE: the *_vert (vertical CJK) Tesseract models are NOT packaged in Debian
# apt — only the horizontal packs below are. The vertical models are fetched
# from the upstream tessdata repo in the next step. (Installing the bogus
# tesseract-ocr-chi-tra-vert / -jpn-vert packages here would fail the build,
# which previously left the image with no working OCR at all.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-core \
        curl ca-certificates gnupg \
        ghostscript \
        tesseract-ocr \
        tesseract-ocr-chi-tra \
        tesseract-ocr-chi-sim \
        tesseract-ocr-jpn \
        tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*

# Vertical CJK OCR models (used for vertical-text books). Not in apt, so pull
# the trained data straight into Tesseract's tessdata directory. tessdata_fast
# keeps OCR quick; swap to tessdata (full) if you want maximum accuracy.
RUN TESSDATA=/usr/share/tesseract-ocr/5/tessdata \
    && curl -fsSL -o "$TESSDATA/chi_tra_vert.traineddata" \
        https://github.com/tesseract-ocr/tessdata_fast/raw/main/chi_tra_vert.traineddata \
    && curl -fsSL -o "$TESSDATA/jpn_vert.traineddata" \
        https://github.com/tesseract-ocr/tessdata_fast/raw/main/jpn_vert.traineddata \
    && tesseract --list-langs

# Node.js (Vivliostyle CLI 11.0.1+ requires Node >= 22.12).
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Vivliostyle CLI (rendering engine). `--version` doubles as a smoke test: if
# the CLI cannot start on this Node, the build fails here rather than at the
# first conversion.
RUN npm install -g "@vivliostyle/cli@${VIVLIOSTYLE_VERSION}" \
    && npm cache clean --force \
    && vivliostyle --version

# Chromium is launched through this wrapper (see CHROMIUM_PATH above) so the
# container-safe flags apply to every render. See the script for why.
COPY scripts/chromium-container.sh /usr/local/bin/chromium-container
RUN chmod +x /usr/local/bin/chromium-container \
    && chromium-container --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8000
# Single worker: the app intentionally serialises conversions (one at a time)
# and keeps job state in memory, so it must run as one process.
#
# --proxy-headers with --forwarded-allow-ips="*" makes uvicorn trust the
# X-Forwarded-Proto/For headers set by the platform's TLS-terminating edge, so
# request.url is https and access logs show the real client IP. The container is
# only reachable through that edge, so trusting every upstream hop is safe here.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
