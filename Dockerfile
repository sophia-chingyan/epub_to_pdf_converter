# ePUB -> PDF converter
# Bundles: Python app + Node 20 (for Vivliostyle CLI) + system Chromium + Noto CJK fonts.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CHROMIUM_PATH=/usr/bin/chromium \
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

# Node.js 20 (Vivliostyle CLI requires Node >= 20).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Vivliostyle CLI (rendering engine). Pin a version here for reproducible
# builds once you have verified one works for your books, e.g.
#   npm install -g @vivliostyle/cli@9.x
RUN npm install -g @vivliostyle/cli && vivliostyle --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8000
# Single worker: the app intentionally serialises conversions (one at a time)
# and keeps job state in memory, so it must run as one process.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
