"""PDF → ePUB conversion.

The PDF is parsed with PyMuPDF and rebuilt as a reflowable EPUB 3 package.
Nothing here shells out except the optional OCR pre-pass (ocrmypdf), which is
reused from the ePUB→PDF pipeline for scanned or PUA-obfuscated inputs.

Pipeline
--------
1. ``validate``        – is it a PDF, is it readable (not password-protected).
2. ``analyze``         – one cheap pass over every page to decide the writing
                          mode (horizontal vs. vertical CJK), the language, the
                          body font size, the heading size ladder and which
                          lines are running headers / footers / page numbers.
3. ``build``           – a second pass that turns glyph geometry into
                          paragraphs, headings, ruby (furigana / bopomofo),
                          hyperlinks, images, tables and chapter files, then
                          packs everything into an EPUB with a navigation
                          document generated from the PDF bookmarks.

Vertical text
-------------
PDF has no notion of "vertical writing mode"; it only has glyph positions.
Two representations are common and both are handled:

* Fonts with ``WMode 1`` (Identity-V) – MuPDF reports the line with
  ``wmode == 1`` and a vertical direction vector.
* Chromium / Skia output (which is what Vivliostyle produces) – every glyph is
  its own horizontal one-character "line", stacked top-to-bottom in a column.

Both are normalised into vertical *runs* (columns) and the document is
declared ``writing-mode: vertical-rl`` with a right-to-left spine when the
majority of the body text is vertical.
"""
from __future__ import annotations

import html
import io
import re
import subprocess
import unicodedata
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pymupdf

from converter import _thumbnail, resolve_ocr_langs


class PdfError(Exception):
    """Raised when a PDF is invalid, encrypted, or otherwise unusable."""


# --- Tunables ---------------------------------------------------------------
HEADING_RATIO = 1.15        # dominant size / body size that makes a unit a heading
HEADING_MAX_CHARS = 160     # longer units are never headings (block quotes etc.)
SMALL_RATIO = 0.85          # ≤ this × body size → class "small"
RUBY_RATIO = 0.65           # ≤ this × base size → ruby candidate
BAND_FRACTION = 0.14        # top/bottom page fraction scanned for repeated headers/footers
BAND_FRACTION_POS = 0.09    # narrower band for the position-only (changing text) rule
MIN_REPEAT_PAGES = 3        # a band line must repeat on this many pages to be dropped
MAX_FILE_BYTES = 250_000    # soft cap per XHTML file (split at a page boundary)
SCANNED_TEXT_COVERAGE = 0.2 # fraction of pages with text below which the PDF is "scanned"
MIN_TEXT_CHARS_PER_PAGE = 20
OCR_TIMEOUT_SEC = 3600

_TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_LIGATURES
    | pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_PRESERVE_IMAGES
    | pymupdf.TEXT_MEDIABOX_CLIP
)

_TERMINAL_PUNCT = set(".!?:;\"')]」』】〉》）］…。！？」』")
_CJK_CLOSERS = set("」』】〉》）］｝、，。！？：；…―")
_HYPHENS = ("-", "‐", "­")

# Characters that appear only (or overwhelmingly) in one Chinese script.
_SIMPLIFIED_ONLY = set("们这个说国时会对发经来学为么后与东车长门问间见开关电书业还产从动无")
_TRADITIONAL_ONLY = set("們這個說國時會對發經來學為麼後與東車長門問間見開關電書業還產從動無")

_IMAGE_EXT_FIX = {"jpx": "jp2", "jpeg": "jpg"}
_IMAGE_MEDIA = {
    "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "jp2": "image/jp2", "bmp": "image/bmp",
    "svg": "image/svg+xml",
}


# --- Character classification ----------------------------------------------
def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x2FDF      # radicals
        or 0x3000 <= cp <= 0x30FF   # CJK punctuation, hiragana, katakana
        or 0x3100 <= cp <= 0x312F   # bopomofo
        or 0x3130 <= cp <= 0x318F   # hangul compatibility jamo
        or 0x31A0 <= cp <= 0x31FF   # bopomofo ext, katakana ext
        or 0x3400 <= cp <= 0x4DBF   # CJK ext A
        or 0x4E00 <= cp <= 0x9FFF   # CJK unified
        or 0xAC00 <= cp <= 0xD7AF   # hangul syllables
        or 0xF900 <= cp <= 0xFAFF   # compatibility ideographs
        or 0xFE30 <= cp <= 0xFE4F   # vertical forms
        or 0xFF00 <= cp <= 0xFFEF   # full-width forms
        or 0x20000 <= cp <= 0x2FA1F # ext B..F
    )


def _is_kana(ch: str) -> bool:
    cp = ord(ch)
    return 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF


def _is_hangul(ch: str) -> bool:
    cp = ord(ch)
    return 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F


def _is_han(ch: str) -> bool:
    cp = ord(ch)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2FA1F or 0xF900 <= cp <= 0xFAFF


def _is_bopomofo(ch: str) -> bool:
    cp = ord(ch)
    return 0x3100 <= cp <= 0x312F or 0x31A0 <= cp <= 0x31BF


def _is_space(ch: str) -> bool:
    """Whitespace, except the ideographic space (U+3000), which is content."""
    return ch.isspace() and ch != "\u3000"


def _is_pua(ch: str) -> bool:
    cp = ord(ch)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD


@dataclass
class ScriptStats:
    han: int = 0
    kana: int = 0
    hangul: int = 0
    latin: int = 0
    simplified: int = 0
    traditional: int = 0
    pua: int = 0
    total: int = 0          # non-space characters

    def add_text(self, text: str) -> None:
        for ch in text:
            if ch.isspace():
                continue
            self.total += 1
            if _is_pua(ch):
                self.pua += 1
            elif _is_kana(ch):
                self.kana += 1
            elif _is_hangul(ch):
                self.hangul += 1
            elif _is_han(ch):
                self.han += 1
                if ch in _SIMPLIFIED_ONLY:
                    self.simplified += 1
                elif ch in _TRADITIONAL_ONLY:
                    self.traditional += 1
            elif ch.isalpha():
                self.latin += 1

    def guess_language(self) -> str:
        """BCP 47 tag from script frequencies (en / ja / ko / zh-TW / zh-CN)."""
        cjk = self.han + self.kana + self.hangul
        if cjk == 0 or self.latin > cjk * 3:
            return "en"
        # Kana is decisive: Japanese text is never without it for long.
        if self.kana > max(cjk * 0.03, 5):
            return "ja"
        if self.hangul > cjk * 0.3:
            return "ko"
        if self.simplified > self.traditional:
            return "zh-CN"
        if self.traditional > self.simplified:
            return "zh-TW"
        return "zh"

    @property
    def cjk_fraction(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.han + self.kana + self.hangul) / self.total

    @property
    def pua_fraction(self) -> float:
        n = self.total - self.latin
        return self.pua / n if n else 0.0


# --- Geometry primitives ----------------------------------------------------
@dataclass(slots=True)
class Glyph:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool = False
    italic: bool = False
    mono: bool = False
    sup: bool = False
    link: str | None = None
    ruby: int = -1              # index into Run.rubies, or -1


@dataclass(slots=True)
class Run:
    """One line (horizontal) or one column (vertical) of glyphs in order."""
    glyphs: list[Glyph]
    vertical: bool
    x0: float
    y0: float
    x1: float
    y1: float
    rubies: list[str] = field(default_factory=list)
    _size: float = -1.0             # cached dominant size; reset by invalidate()

    @property
    def text(self) -> str:
        return "".join(g.c for g in self.glyphs)

    @property
    def size(self) -> float:
        if self._size < 0:
            self._size = _mode(round(g.size * 2) / 2 for g in self.glyphs) if self.glyphs else 0.0
        return self._size

    def invalidate(self) -> None:
        """Call after replacing ``glyphs``: recomputes the bbox and size cache."""
        self._size = -1.0
        if self.glyphs:
            self.x0, self.y0 = min(g.x0 for g in self.glyphs), min(g.y0 for g in self.glyphs)
            self.x1, self.y1 = max(g.x1 for g in self.glyphs), max(g.y1 for g in self.glyphs)

    @property
    def is_cjk(self) -> bool:
        n = sum(1 for g in self.glyphs if not _is_space(g.c))
        return n > 0 and sum(1 for g in self.glyphs if is_cjk(g.c)) >= n * 0.5

    def start(self) -> float:
        """Position along the flow axis where the run begins."""
        return self.y0 if self.vertical else self.x0

    def end(self) -> float:
        return self.y1 if self.vertical else self.x1


@dataclass(slots=True)
class Unit:
    """A block-level thing on a page, in reading order: text, image, or table."""
    kind: str                       # "text" | "image" | "table"
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    runs: list[Run] = field(default_factory=list)
    image: "ImageRef | None" = None
    html: str = ""                  # pre-rendered (tables)
    ids: list[str] = field(default_factory=list)
    _size: float = -1.0

    @property
    def size(self) -> float:
        if self._size < 0:
            glyphs = [g for r in self.runs for g in r.glyphs if not _is_space(g.c)]
            self._size = _mode(round(g.size * 2) / 2 for g in glyphs) if glyphs else 0.0
        return self._size

    @property
    def nchars(self) -> int:
        return sum(1 for r in self.runs for g in r.glyphs if not _is_space(g.c))

    @property
    def vertical(self) -> bool:
        return bool(self.runs) and sum(1 for r in self.runs if r.vertical) * 2 >= len(self.runs)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class ImageRef:
    name: str            # file name inside OEBPS/images/
    media_type: str
    data: bytes
    width: int
    height: int


def _mode(values: Iterable[float]) -> float:
    c = Counter(values)
    if not c:
        return 0.0
    # Most common; ties broken by the larger value (rare, deterministic).
    best = max(c.items(), key=lambda kv: (kv[1], kv[0]))
    return best[0]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


# --- Validation & metadata --------------------------------------------------
def validate(pdf_path: Path) -> None:
    """Reject files that are not PDFs or cannot be opened without a password."""
    try:
        with pdf_path.open("rb") as fh:
            head = fh.read(1024)
    except OSError as e:
        raise PdfError(f"Cannot read file: {e}")
    if b"%PDF" not in head:
        raise PdfError("This file is not a valid PDF (missing %PDF header).")
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:
        raise PdfError(f"This file is not a valid PDF: {e}")
    try:
        if doc.needs_pass:
            raise PdfError("This PDF is password-protected and cannot be converted.")
        if doc.page_count == 0:
            raise PdfError("This PDF has no pages.")
        if not doc.is_pdf:
            raise PdfError("This file is not a PDF.")
    finally:
        doc.close()


@dataclass
class PdfInfo:
    title: str
    author: str = ""
    language: str | None = None      # from the PDF catalog /Lang, if any
    page_count: int = 0
    cover_bytes: bytes | None = None
    cover_ext: str | None = None
    warnings: list[str] = field(default_factory=list)


def _catalog_lang(doc: pymupdf.Document) -> str | None:
    try:
        kind, val = doc.xref_get_key(doc.pdf_catalog(), "Lang")
    except Exception:
        return None
    if kind == "string" and val:
        return val.strip() or None
    return None


def _clean_title(raw: str | None, fallback: str) -> str:
    t = (raw or "").strip()
    # Producers frequently leave the source file name as the title.
    if not t or re.fullmatch(r"(?i)(untitled|microsoft word - .*|.*\.(docx?|html?|pdf|indd|tex|odt))", t):
        return fallback
    return t


def extract_info(pdf_path: Path) -> PdfInfo:
    """Title, author, catalog language and a cover thumbnail (page 1)."""
    doc = pymupdf.open(str(pdf_path))
    try:
        meta = doc.metadata or {}
        info = PdfInfo(
            title=_clean_title(meta.get("title"), pdf_path.stem),
            author=(meta.get("author") or "").strip(),
            language=_catalog_lang(doc),
            page_count=doc.page_count,
        )
        try:
            cover = cover_image(doc)
            if cover:
                info.cover_bytes, info.cover_ext = _thumbnail(cover.data, "cover." + cover.name.rsplit(".", 1)[-1])
        except Exception:
            info.warnings.append("Could not render a cover thumbnail.")
        return info
    finally:
        doc.close()


def cover_image(doc: pymupdf.Document, *, max_height: int = 1600) -> ImageRef | None:
    """Return the cover: page 1's own full-page image if it has one, else a render."""
    if doc.page_count == 0:
        return None
    page = doc[0]
    prect = page.rect
    if prect.is_empty:
        return None
    text_chars = len((page.get_text("text") or "").strip())
    # A page that is (almost) only one big image: use that image at full quality.
    if text_chars < 50:
        try:
            for info in page.get_image_info(xrefs=True):
                r = pymupdf.Rect(info["bbox"])
                if r.get_area() >= prect.get_area() * 0.7 and info.get("xref"):
                    ref = _extract_image(doc, info["xref"], "cover")
                    if ref and ref.width >= 200:
                        return ref
        except Exception:
            pass
    zoom = min(2.0, max_height / max(prect.height, 1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    data = pix.tobytes("jpeg", jpg_quality=85)
    return ImageRef("cover.jpg", "image/jpeg", data, pix.width, pix.height)


def _extract_image(doc: pymupdf.Document, xref: int, stem: str) -> ImageRef | None:
    """Extract image *xref*, folding a soft mask into a PNG when present."""
    try:
        base = doc.extract_image(xref)
    except Exception:
        return None
    if not base or not base.get("image"):
        return None
    ext = _IMAGE_EXT_FIX.get(base.get("ext", ""), base.get("ext", ""))
    smask = base.get("smask") or 0
    data = base["image"]
    if smask or ext not in ("jpg", "png", "gif", "webp"):
        # Recombine with alpha, or re-encode exotic formats (jp2/jbig2/ccitt).
        try:
            pix = pymupdf.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4 or pix.colorspace is None or pix.colorspace.n not in (1, 3):
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if smask:
                mask = pymupdf.Pixmap(doc, smask)
                if mask.width == pix.width and mask.height == pix.height:
                    if pix.alpha:
                        pix = pymupdf.Pixmap(pix, 0)
                    pix = pymupdf.Pixmap(pix, mask)
            data = pix.tobytes("png")
            ext = "png"
        except Exception:
            if ext not in _IMAGE_MEDIA:
                return None
    return ImageRef(f"{stem}.{ext}", _IMAGE_MEDIA.get(ext, "application/octet-stream"),
                    data, int(base.get("width", 0)), int(base.get("height", 0)))


# --- Pass 1: document analysis ----------------------------------------------
@dataclass
class Analysis:
    vertical: bool = False
    language: str = "en"
    body_size: float = 12.0
    heading_sizes: list[float] = field(default_factory=list)   # descending
    drop_signatures: set[tuple[str, str]] = field(default_factory=set)
    drop_positions: set[tuple[str, int]] = field(default_factory=set)
    pages_with_text: int = 0
    page_count: int = 0
    scripts: ScriptStats = field(default_factory=ScriptStats)
    has_bookmarks: bool = False

    @property
    def text_coverage(self) -> float:
        return self.pages_with_text / self.page_count if self.page_count else 0.0

    def heading_level(self, size: float) -> int:
        for i, s in enumerate(self.heading_sizes):
            if size >= s - 0.01:
                return min(i + 1, 4)
        return 4


def _band_signature(text: str) -> str:
    """Normalise a header/footer line so page numbers collapse to one key."""
    t = unicodedata.normalize("NFKC", text).strip().lower()
    t = re.sub(r"[0-9〇一二三四五六七八九十百千]+", "#", t)
    t = re.sub(r"\b[ivxlcdm]+\b", "#", t)
    t = re.sub(r"\s+", " ", t)
    return t


_PAGE_NUMBER_RE = re.compile(
    r"^[\s\-–—・·|]*(?:page|p\.|頁|ページ|第)?[\s\-–—・]*[0-9〇一二三四五六七八九十百千]+[\s\-–—・|]*(?:頁|ページ|ページ目|/\s*[0-9]+)?[\s\-–—・|]*$",
    re.IGNORECASE,
)


def _page_dict(page: pymupdf.Page, raw: bool) -> dict:
    d = page.get_text("rawdict" if raw else "dict", flags=_TEXT_FLAGS)
    if page.rotation:
        m = page.rotation_matrix
        for b in d["blocks"]:
            b["bbox"] = tuple(pymupdf.Rect(b["bbox"]) * m)
            for l in b.get("lines", []):
                l["bbox"] = tuple(pymupdf.Rect(l["bbox"]) * m)
                for s in l["spans"]:
                    s["bbox"] = tuple(pymupdf.Rect(s["bbox"]) * m)
                    for c in s.get("chars", []):
                        c["bbox"] = tuple(pymupdf.Rect(c["bbox"]) * m)
    return d


def _line_is_vertical(line: dict) -> bool:
    if line.get("wmode") == 1:
        return True
    dx, dy = line.get("dir", (1, 0))
    return abs(dy) > abs(dx)


def _block_is_stacked(lines: list[dict]) -> bool:
    """Chromium-style vertical text: one-glyph lines stacked in a column."""
    if len(lines) < 3:
        return False
    short = 0
    stacked = 0
    cjk = 0
    total = 0
    prev = None
    for l in lines:
        text = "".join(s["text"] if "text" in s else "".join(c["c"] for c in s["chars"]) for s in l["spans"]).strip()
        total += len(text)
        cjk += sum(1 for ch in text if is_cjk(ch))
        if len(text) <= 2:
            short += 1
        if prev is not None:
            px0, py0, px1, py1 = prev
            x0, y0, x1, y1 = l["bbox"]
            if y0 >= py0 - 1 and _overlap(px0, px1, x0, x1) >= 0.5 * min(px1 - px0, x1 - x0, 1e9):
                stacked += 1
        prev = l["bbox"]
    return (short >= 0.8 * len(lines) and stacked >= 0.8 * (len(lines) - 1)
            and total > 0 and cjk >= 0.5 * total)


def analyze(pdf_path: Path, progress_cb: Callable[[str], None] | None = None) -> Analysis:
    """Cheap first pass over the document (see module docstring)."""
    doc = pymupdf.open(str(pdf_path))
    try:
        an = Analysis(page_count=doc.page_count)
        an.has_bookmarks = bool(doc.get_toc())
        size_hist: Counter[float] = Counter()
        vertical_chars = 0
        horizontal_chars = 0
        band_hits: dict[tuple[str, str], set[int]] = defaultdict(set)
        # (where, rounded y0) → {page: (size, nchars)} for small/short band lines
        band_pos: dict[tuple[str, int], dict[int, tuple[float, int]]] = defaultdict(dict)

        for pno, page in enumerate(doc):
            if progress_cb and (pno % 10 == 0 or pno == doc.page_count - 1):
                progress_cb(f"Analysing layout (page {pno + 1}/{doc.page_count})")
            prect = page.rect
            band = prect.height * BAND_FRACTION
            d = _page_dict(page, raw=False)
            page_chars = 0
            for b in d["blocks"]:
                if b["type"] != 0:
                    continue
                lines = b["lines"]
                stacked = _block_is_stacked(lines)
                btext_parts = []
                for l in lines:
                    ltext = "".join(s["text"] for s in l["spans"])
                    btext_parts.append(ltext)
                    n = len(ltext.strip())
                    page_chars += n
                    if stacked or _line_is_vertical(l):
                        vertical_chars += n
                    else:
                        horizontal_chars += n
                    for s in l["spans"]:
                        t = s["text"].strip()
                        if t:
                            size_hist[round(s["size"] * 2) / 2] += len(t)
                            an.scripts.add_text(t)
                btext = "".join(btext_parts).strip()
                x0, y0, x1, y1 = b["bbox"]
                if btext and (y1 <= prect.y0 + band or y0 >= prect.y1 - band):
                    where = "top" if y1 <= prect.y0 + band else "bottom"
                    band_hits[(where, _band_signature(btext))].add(pno)
                    bsize = _mode(round(s["size"] * 2) / 2 for l in lines for s in l["spans"] if s["text"].strip())
                    narrow = prect.height * BAND_FRACTION_POS
                    if (where == "top" and y1 <= prect.y0 + narrow) or (where == "bottom" and y0 >= prect.y1 - narrow):
                        band_pos[(where, _pos_key(y0 if where == "top" else y1))][pno] = (bsize, len(btext))
            if page_chars >= MIN_TEXT_CHARS_PER_PAGE:
                an.pages_with_text += 1

        an.vertical = vertical_chars > horizontal_chars and vertical_chars > 0
        an.language = an.scripts.guess_language()

        if size_hist:
            an.body_size = _mode_weighted(size_hist)
        heads = sorted({s for s in size_hist if s >= an.body_size * HEADING_RATIO}, reverse=True)
        an.heading_sizes = heads

        min_pages = max(MIN_REPEAT_PAGES, int(doc.page_count * 0.25))
        if doc.page_count <= 2:
            min_pages = 99  # never treat lines of a 1–2 page document as running heads
        for key, pages in band_hits.items():
            if len(pages) >= min_pages:
                an.drop_signatures.add(key)
        # Running heads whose text changes per chapter: same slot on many pages,
        # smaller than body text (or very short), e.g. "Chapter One  ·  17".
        pos_min = max(min_pages, int(doc.page_count * 0.4))
        for key, hits in band_pos.items():
            small = [pno for pno, (sz, n) in hits.items() if sz < an.body_size * 0.92 or n <= 40]
            if len(small) >= pos_min:
                an.drop_positions.add(key)
        return an
    finally:
        doc.close()


def _pos_key(y: float) -> int:
    return int(round(y / 3.0))


def _mode_weighted(hist: Counter) -> float:
    # Ties go to the smaller size: body text is never larger than its headings.
    return max(hist.items(), key=lambda kv: (kv[1], -kv[0]))[0]


# --- OCR pre-pass (scanned / obfuscated PDFs) -------------------------------
def ocr_scanned_pdf(pdf_path: Path, *, langs: str, jobs: int) -> bool:
    """Add a text layer to a scanned PDF with ocrmypdf --skip-text. In place."""
    langs, _dropped = resolve_ocr_langs(langs)
    if not langs:
        return False
    out = pdf_path.with_suffix(".ocr.pdf")
    cmd = ["ocrmypdf", "-l", langs, "--jobs", str(jobs), "--output-type", "pdf",
           "--skip-text", str(pdf_path), str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OCR_TIMEOUT_SEC)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            out.replace(pdf_path)
            return True
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        if tail:
            print("[pdf2epub] ocr failed: " + " | ".join(tail))
    except subprocess.TimeoutExpired:
        print(f"[pdf2epub] ocr timed out after {OCR_TIMEOUT_SEC}s")
    except FileNotFoundError:
        print("[pdf2epub] ocrmypdf not found on PATH")
    finally:
        out.unlink(missing_ok=True)
    return False


def prepare_text_layer(
    pdf_path: Path,
    an: Analysis,
    *,
    mode: str,
    langs: str,
    jobs: int,
    pua_threshold: float,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[Analysis, str | None]:
    """Run OCR when the PDF is scanned or PUA-obfuscated. Returns (analysis, note).

    *mode* is "auto" or "off". The PDF is modified in place and re-analysed on
    success. A note is returned for the library sidecar when something
    noteworthy happened (OCR applied, or needed but unavailable).
    """
    if mode == "off" or an.page_count == 0:
        return an, None

    if an.text_coverage < SCANNED_TEXT_COVERAGE:
        if progress_cb:
            progress_cb("Scanned PDF detected — running OCR")
        if ocr_scanned_pdf(pdf_path, langs=langs, jobs=jobs):
            new = analyze(pdf_path, progress_cb)
            if new.text_coverage >= SCANNED_TEXT_COVERAGE:
                return new, "text recovered via OCR (scanned PDF)"
            return new, "OCR ran but recovered little text; pages kept as images"
        return an, "scanned PDF; OCR unavailable, pages kept as images"

    if an.scripts.pua_fraction >= pua_threshold:
        from converter import add_text_layer
        if progress_cb:
            progress_cb("Obfuscated text layer detected — running OCR")
        ok = add_text_layer(
            pdf_path, langs=langs,
            page_direction="rtl" if an.vertical else None,
            reason="pua", pua_threshold=pua_threshold,
        )
        if ok:
            return analyze(pdf_path, progress_cb), "text layer rebuilt via OCR (PUA-obfuscated PDF)"
        return an, "text layer is PUA-obfuscated and OCR failed; text may be garbled"
    return an, None


# --- Pass 2a: page → runs & units -------------------------------------------
def _span_style(span: dict) -> tuple[bool, bool, bool, bool]:
    flags = span.get("flags", 0)
    font = (span.get("font") or "").lower()
    bold = bool(flags & 16) or any(k in font for k in ("bold", "black", "heavy", "semibold", "demibold"))
    italic = bool(flags & 2) or "italic" in font or "oblique" in font
    mono = bool(flags & 8)
    sup = bool(flags & 1)
    return bold, italic, mono, sup


def _glyphs_from_span(span: dict) -> list[Glyph]:
    bold, italic, mono, sup = _span_style(span)
    size = float(span.get("size", 0.0))
    out: list[Glyph] = []
    for c in span.get("chars", []):
        ch = c["c"]
        if ch == "�":
            continue
        x0, y0, x1, y1 = c["bbox"]
        out.append(Glyph(ch, x0, y0, x1, y1, size, bold, italic, mono, sup))
    return out


def _run_from_glyphs(glyphs: list[Glyph], vertical: bool) -> Run | None:
    if not glyphs:
        return None
    return Run(
        glyphs, vertical,
        min(g.x0 for g in glyphs), min(g.y0 for g in glyphs),
        max(g.x1 for g in glyphs), max(g.y1 for g in glyphs),
    )


GAP_SPLIT_RATIO = 4.0   # a gap wider than this × font size splits a line into two runs
GUTTER_MIN_RATIO = 0.6  # a page-wide empty x-stripe at least this × body size is a column gutter


def _split_at_gaps(glyphs: list[Glyph], vertical: bool) -> list[list[Glyph]]:
    """Split a line at gaps that can only be a column gutter or a table cell."""
    parts: list[list[Glyph]] = []
    cur: list[Glyph] = []
    last: Glyph | None = None
    for g in glyphs:
        if _is_space(g.c):
            if cur:
                cur.append(g)
            continue
        if last is not None:
            gap = (g.y0 - last.y1) if vertical else (g.x0 - last.x1)
            if gap > GAP_SPLIT_RATIO * max(last.size, g.size, 1.0):
                while cur and _is_space(cur[-1].c):
                    cur.pop()
                if cur:
                    parts.append(cur)
                cur = []
        cur.append(g)
        last = g
    while cur and _is_space(cur[-1].c):
        cur.pop()
    if cur:
        parts.append(cur)
    return parts


def _find_gutters(blocks: list[dict], body_size: float) -> list[tuple[float, float]]:
    """x-ranges that are empty on (nearly) every horizontal line crossing them.

    Word spaces never line up across dozens of lines; a column gutter does.
    Lines that do have ink there (a full-width heading) simply are not split.
    """
    lines: list[tuple[float, float, list[tuple[float, float]]]] = []
    for b in blocks:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            if _line_is_vertical(l):
                continue
            ivs = [(c["bbox"][0], c["bbox"][2]) for s in l["spans"] for c in s["chars"] if not _is_space(c["c"])]
            if len(ivs) < 2:
                continue
            lines.append((min(a for a, _ in ivs), max(b for _, b in ivs), ivs))
    if len(lines) < 6:
        return []
    xmin = min(l[0] for l in lines)
    xmax = max(l[1] for l in lines)
    step = 1.0
    nbins = int((xmax - xmin) / step) + 1
    cover = [0] * nbins
    empty = [0] * nbins
    for x0, x1, ivs in lines:
        b0, b1 = int((x0 - xmin) / step), int((x1 - xmin) / step)
        inked = [False] * (b1 - b0 + 1)
        for a, b in ivs:
            for k in range(max(b0, int((a - xmin) / step)), min(b1, int((b - xmin) / step)) + 1):
                inked[k - b0] = True
        for k in range(b0, b1 + 1):
            cover[k] += 1
            if not inked[k - b0]:
                empty[k] += 1
    min_cover = max(5, int(len(lines) * 0.3))
    gutters: list[tuple[float, float]] = []
    start = None
    for k in range(nbins + 1):
        is_gap = k < nbins and cover[k] >= min_cover and empty[k] >= 0.9 * cover[k]
        if is_gap and start is None:
            start = k
        elif not is_gap and start is not None:
            width = (k - start) * step
            if width >= GUTTER_MIN_RATIO * body_size:
                gutters.append((xmin + start * step, xmin + k * step))
            start = None
    return gutters


def _split_at_gutters(glyphs: list[Glyph], gutters: list[tuple[float, float]]) -> list[list[Glyph]]:
    if not gutters:
        return [glyphs]
    parts: list[list[Glyph]] = [[]]
    last: Glyph | None = None
    for g in glyphs:
        if last is not None and not _is_space(g.c):
            for gx0, gx1 in gutters:
                if last.x1 <= gx0 + 0.5 and g.x0 >= gx1 - 0.5:
                    while parts[-1] and _is_space(parts[-1][-1].c):
                        parts[-1].pop()
                    parts.append([])
                    break
        if _is_space(g.c) and not parts[-1]:
            continue
        parts[-1].append(g)
        if not _is_space(g.c):
            last = g
    return [p for p in parts if p]


def _merge_baseline_lines(lines: list[dict]) -> list[dict]:
    """Re-join horizontal 'lines' MuPDF split at wide (justified) word gaps.

    Two lines of a block that share a baseline and sit side by side are one
    typographic line. A synthetic space is inserted between them; column
    gutters are cut again afterwards by _split_at_gutters.
    """
    out: list[dict] = []
    for l in lines:
        if _line_is_vertical(l) or not out or _line_is_vertical(out[-1]):
            out.append(l)
            continue
        prev = out[-1]
        px0, py0, px1, py1 = prev["bbox"]
        x0, y0, x1, y1 = l["bbox"]
        h = min(py1 - py0, y1 - y0)
        size = max((s.get("size", 0) for s in l["spans"]), default=0)
        if h > 0 and _overlap(py0, py1, y0, y1) >= 0.6 * h and 0 <= x0 - px1 <= GAP_SPLIT_RATIO * size:
            spans = prev["spans"]
            if spans and spans[-1].get("chars"):
                last = spans[-1]["chars"][-1]
                lx0, ly0, lx1, ly1 = last["bbox"]
                spans[-1]["chars"].append({"c": " ", "bbox": (lx1, ly0, x0, ly1), "origin": (lx1, ly1), "synthetic": True})
            prev["spans"] = spans + l["spans"]
            prev["bbox"] = (min(px0, x0), min(py0, y0), max(px1, x1), max(py1, y1))
            continue
        out.append(l)
    return out


def _block_to_runs(block: dict, gutters: list[tuple[float, float]] | None = None) -> list[Run]:
    lines = block["lines"]
    if _block_is_stacked(lines):
        glyphs = [g for l in lines for s in l["spans"] for g in _glyphs_from_span(s)]
        glyphs = [g for g in glyphs if not _is_space(g.c)]
        glyphs.sort(key=lambda g: g.y0)
        return [r for part in _split_at_gaps(glyphs, True) if (r := _run_from_glyphs(part, True))]
    runs: list[Run] = []
    for l in _merge_baseline_lines(lines):
        glyphs = [g for s in l["spans"] for g in _glyphs_from_span(s)]
        if not glyphs or not "".join(g.c for g in glyphs).strip():
            continue
        vertical = _line_is_vertical(l) and sum(1 for g in glyphs if is_cjk(g.c)) * 2 >= len(glyphs)
        if vertical:
            glyphs = [g for g in glyphs if not _is_space(g.c)]
            glyphs.sort(key=lambda g: g.y0)
        else:
            # Trim edge whitespace; collapse interior runs of spaces.
            while glyphs and _is_space(glyphs[0].c):
                glyphs.pop(0)
            while glyphs and _is_space(glyphs[-1].c):
                glyphs.pop()
            cleaned: list[Glyph] = []
            for g in glyphs:
                if _is_space(g.c) and cleaned and _is_space(cleaned[-1].c):
                    continue
                if _is_space(g.c):
                    g.c = " "
                cleaned.append(g)
            glyphs = cleaned
        pieces = [glyphs] if vertical else _split_at_gutters(glyphs, gutters or [])
        for piece in pieces:
            for part in _split_at_gaps(piece, vertical):
                r = _run_from_glyphs(part, vertical)
                if r:
                    runs.append(r)
    return runs


def _cluster_runs(runs: list[Run]) -> list[list[Run]]:
    """Group one block's runs into units: runs that sit in the same column.

    MuPDF happily joins the left and right halves of a two-column page into
    one line (they share a baseline) and hence one block. After the halves
    are split at the gutter, this regroups them by overlap on the cross axis.
    """
    n = len(runs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = runs[i], runs[j]
            if a.vertical != b.vertical:
                continue
            if a.vertical:
                ov = _overlap(a.y0, a.y1, b.y0, b.y1)
                near = min(abs(a.x0 - b.x1), abs(b.x0 - a.x1)) <= max(a.size, b.size) * 2.5 or _overlap(a.x0, a.x1, b.x0, b.x1) > 0
                ok = ov >= 0.3 * min(a.y1 - a.y0, b.y1 - b.y0, 1e9) and near
            else:
                ov = _overlap(a.x0, a.x1, b.x0, b.x1)
                near = min(abs(a.y0 - b.y1), abs(b.y0 - a.y1)) <= max(a.size, b.size) * 2.5 or _overlap(a.y0, a.y1, b.y0, b.y1) > 0
                ok = ov >= 0.3 * min(a.x1 - a.x0, b.x1 - b.x0, 1e9) and near
            if ok:
                parent[find(i)] = find(j)
    groups: dict[int, list[Run]] = defaultdict(list)
    for i, r in enumerate(runs):
        groups[find(i)].append(r)
    return [g for _, g in sorted(groups.items(), key=lambda kv: min(runs.index(r) for r in kv[1]))]


def _attach_ruby(units: list[Unit], body_size: float) -> list[Unit]:
    """Fold small kana / bopomofo runs into <ruby> annotations on their base run.

    A ruby candidate is a run whose glyphs are ≤ RUBY_RATIO × the size of an
    adjacent, larger run that it sits beside: to the right of a vertical
    column, or directly above a horizontal line. The base glyphs are those
    overlapping the ruby's extent along the flow axis.
    """
    all_runs = [(u, r) for u in units if u.kind == "text" for r in u.runs]
    bases = [(u, r) for u, r in all_runs if r.size >= body_size * 0.8]
    consumed: set[int] = set()
    # A ruby line above a horizontal base often carries several annotations
    # separated by spaces ("ㄩˋ ㄕㄢ"); split those so each finds its own base.
    split_runs: list[tuple[Unit, Run]] = []
    for u, r in all_runs:
        if r.size <= body_size * RUBY_RATIO and any(_is_space(g.c) for g in r.glyphs):
            parts: list[list[Glyph]] = [[]]
            for g in r.glyphs:
                if _is_space(g.c):
                    if parts[-1]:
                        parts.append([])
                else:
                    parts[-1].append(g)
            subs = [sr for part in parts if part and (sr := _run_from_glyphs(part, r.vertical))]
            if len(subs) > 1:
                r.rubies = ["__split__"]
                split_runs.extend((u, sr) for sr in subs)
                continue
        split_runs.append((u, r))
    all_runs = split_runs

    for u, r in all_runs:
        if id(r) in consumed or not r.glyphs:
            continue
        rs = r.size
        if rs <= 0 or rs > body_size * RUBY_RATIO:
            continue
        text = r.text.strip()
        if not text or not all(_is_kana(ch) or _is_bopomofo(ch) or ch in "ー゛゜ˊˇˋ˙・" or ch.isalpha() for ch in text):
            continue
        best: tuple[float, Run] | None = None
        for bu, b in bases:
            if b is r or b.size < rs * 1.4 or b.vertical != r.vertical:
                continue
            if r.vertical:
                # ruby column immediately right of the base column, overlapping in y
                if not (b.x1 - 1.5 <= r.x0 <= b.x1 + b.size * 0.6):
                    continue
                ov = _overlap(r.y0, r.y1, b.y0, b.y1)
                if ov < 0.5 * (r.y1 - r.y0):
                    continue
                dist = r.x0 - b.x1
            else:
                # ruby line directly above the base line, overlapping in x
                if not (b.y0 - b.size * 0.8 <= r.y1 <= b.y0 + b.size * 0.35):
                    continue
                ov = _overlap(r.x0, r.x1, b.x0, b.x1)
                if ov < 0.5 * (r.x1 - r.x0):
                    continue
                dist = b.y0 - r.y1
            if best is None or dist < best[0]:
                best = (dist, b)
        if best is None:
            continue
        base = best[1]
        tol = base.size * 0.3
        if r.vertical:
            idx = [i for i, g in enumerate(base.glyphs)
                   if _overlap(g.y0, g.y1, r.y0, r.y1) > 0.25 * (g.y1 - g.y0) or (r.y0 - tol <= g.y0 and g.y1 <= r.y1 + tol)]
        else:
            idx = [i for i, g in enumerate(base.glyphs)
                   if _overlap(g.x0, g.x1, r.x0, r.x1) > 0.25 * (g.x1 - g.x0) or (r.x0 - tol <= g.x0 and g.x1 <= r.x1 + tol)]
        idx = [i for i in idx if not _is_space(base.glyphs[i].c) and base.glyphs[i].ruby < 0]
        if not idx:
            # nearest glyph along the flow axis
            mid = (r.y0 + r.y1) / 2 if r.vertical else (r.x0 + r.x1) / 2
            i = min(range(len(base.glyphs)),
                    key=lambda k: abs(((base.glyphs[k].y0 + base.glyphs[k].y1) / 2 if r.vertical
                                       else (base.glyphs[k].x0 + base.glyphs[k].x1) / 2) - mid))
            idx = [i]
        # keep contiguous span only
        lo, hi = min(idx), max(idx)
        rid = len(base.rubies)
        base.rubies.append(text)
        for i in range(lo, hi + 1):
            base.glyphs[i].ruby = rid
        consumed.add(id(r))
        r.rubies = ["__consumed__"]

    if not consumed:
        return units
    out: list[Unit] = []
    for u in units:
        if u.kind != "text":
            out.append(u)
            continue
        # Drop consumed ruby runs; a split ruby line is dropped if any part
        # attached, otherwise it is kept as ordinary (small) text.
        u.runs = [r for r in u.runs if not (r.rubies == ["__consumed__"] or r.rubies == ["__split__"])]
        if u.runs:
            _refit(u)
            out.append(u)
    return out


def _refit(u: Unit) -> None:
    u._size = -1.0
    u.x0 = min(r.x0 for r in u.runs)
    u.y0 = min(r.y0 for r in u.runs)
    u.x1 = max(r.x1 for r in u.runs)
    u.y1 = max(r.y1 for r in u.runs)


def _attach_links(units: list[Unit], links: list[dict], page: pymupdf.Page) -> None:
    """Tag glyphs covered by link annotations with an href."""
    if not links:
        return
    m = page.rotation_matrix if page.rotation else None
    rects: list[tuple[pymupdf.Rect, str]] = []
    for lk in links:
        href: str | None = None
        kind = lk.get("kind")
        if kind == pymupdf.LINK_URI and lk.get("uri"):
            href = lk["uri"]
        elif kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and lk.get("page", -1) >= 0:
            href = f"#__page_{lk['page']}"
        if not href:
            continue
        r = pymupdf.Rect(lk["from"])
        if m:
            r = r * m
        rects.append((r, href))
    if not rects:
        return
    hits: Counter[int] = Counter()
    for u in units:
        if u.kind != "text":
            continue
        for run in u.runs:
            for g in run.glyphs:
                cx, cy = (g.x0 + g.x1) / 2, (g.y0 + g.y1) / 2
                for k, (r, href) in enumerate(rects):
                    if r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1:
                        g.link = href
                        hits[k] += 1
                        break
    # Chromium places link annotations of vertical text one column off. If a
    # rectangle hit nothing, match it by its extent along the flow axis to the
    # nearest vertical column instead.
    vruns = [r for u in units if u.kind == "text" for r in u.runs if r.vertical]
    for k, (rect, href) in enumerate(rects):
        if hits[k] or not vruns:
            continue
        best: tuple[float, Run] | None = None
        for run in vruns:
            ov = _overlap(rect.y0, rect.y1, run.y0, run.y1)
            if ov < 0.6 * (rect.y1 - rect.y0):
                continue
            dx = abs((run.x0 + run.x1) / 2 - (rect.x0 + rect.x1) / 2)
            if dx <= run.size * 3 and (best is None or dx < best[0]):
                best = (dx, run)
        if best is None:
            continue
        for g in best[1].glyphs:
            cy = (g.y0 + g.y1) / 2
            if rect.y0 - 1 <= cy <= rect.y1 + 1 and g.link is None:
                g.link = href


def _image_units(doc: pymupdf.Document, page: pymupdf.Page, blocks: list[dict],
                 pno: int, page_text_chars: int, seen: dict[bytes, ImageRef],
                 counter: list[int]) -> list[Unit]:
    """Image blocks of the page as units; identical bytes are stored once."""
    import hashlib

    prect = page.rect
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    m = page.rotation_matrix if page.rotation else None
    out: list[Unit] = []
    for b in blocks:
        if b["type"] != 1:
            continue
        x0, y0, x1, y1 = b["bbox"]
        w, h = x1 - x0, y1 - y0
        if w < 4 or h < 4 or b.get("width", 0) < 4 or b.get("height", 0) < 4:
            continue  # spacer / hairline
        area_frac = (w * h) / max(prect.get_area(), 1)
        if area_frac >= 0.85 and page_text_chars >= 200:
            continue  # background scan under an OCR text layer: keep the text
        ref: ImageRef | None = None
        # Prefer the xref route: it recombines soft masks and re-encodes JPX.
        for info in infos:
            r = pymupdf.Rect(info["bbox"])
            if m:
                r = r * m
            if abs(r.x0 - x0) < 2 and abs(r.y0 - y0) < 2 and abs(r.x1 - x1) < 2 and abs(r.y1 - y1) < 2 and info.get("xref"):
                key = b"xref:%d" % info["xref"]
                if key in seen:
                    ref = seen[key]
                else:
                    ref = _extract_image(doc, info["xref"], f"img{counter[0] + 1:04d}")
                    if ref:
                        counter[0] += 1
                        seen[key] = ref
                break
        if ref is None:
            data = b.get("image")
            if not data:
                continue
            key = hashlib.md5(data).digest()
            if key in seen:
                ref = seen[key]
            else:
                ext = _IMAGE_EXT_FIX.get(b.get("ext", "png"), b.get("ext", "png"))
                if ext not in _IMAGE_MEDIA:
                    try:
                        pix = pymupdf.Pixmap(data)
                        data, ext = pix.tobytes("png"), "png"
                    except Exception:
                        continue
                counter[0] += 1
                ref = ImageRef(f"img{counter[0]:04d}.{ext}", _IMAGE_MEDIA[ext], data,
                               int(b.get("width", 0)), int(b.get("height", 0)))
                seen[key] = ref
        out.append(Unit("image", x0, y0, x1, y1, pno, image=ref))
    return out


def _drawing_units(page: pymupdf.Page, text_units: list[Unit], pno: int,
                   counter: list[int], exclude: list[Unit] | None = None) -> list[Unit]:
    """Rasterise clusters of vector drawings (charts, diagrams) that hold no text."""
    prect = page.rect
    try:
        clusters = page.cluster_drawings()
    except Exception:
        return []
    out: list[Unit] = []
    glyph_boxes = [(g.x0, g.y0, g.x1, g.y1) for u in text_units for r in u.runs for g in r.glyphs if not _is_space(g.c)]
    for rect in clusters:
        rect = pymupdf.Rect(rect)
        frac = rect.get_area() / max(prect.get_area(), 1)
        if frac < 0.02 or frac > 0.9 or rect.width < 20 or rect.height < 20:
            continue
        # The rules of a table already emitted as <table>.
        if any((rect & pymupdf.Rect(t.x0, t.y0, t.x1, t.y1)).get_area() >= 0.5 * rect.get_area()
               for t in (exclude or [])):
            continue
        inside = sum(1 for (x0, y0, x1, y1) in glyph_boxes
                     if rect.x0 <= (x0 + x1) / 2 <= rect.x1 and rect.y0 <= (y0 + y1) / 2 <= rect.y1)
        if inside > 12:
            continue  # a table, a box around text, a rule under a paragraph…
        try:
            clip = rect & prect
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
            data = pix.tobytes("png")
        except Exception:
            continue
        counter[0] += 1
        ref = ImageRef(f"fig{counter[0]:04d}.png", "image/png", data, pix.width, pix.height)
        out.append(Unit("image", rect.x0, rect.y0, rect.x1, rect.y1, pno, image=ref))
        # Remove the (few) glyphs that fall inside – they're part of the raster now.
        for u in text_units:
            for r in u.runs:
                r.glyphs = [g for g in r.glyphs
                            if not (rect.x0 <= (g.x0 + g.x1) / 2 <= rect.x1 and rect.y0 <= (g.y0 + g.y1) / 2 <= rect.y1)]
                r.invalidate()
    return out


def _table_units(page: pymupdf.Page, text_units: list[Unit], pno: int) -> list[Unit]:
    """Detect ruled tables and emit them as <table> units, removing their text."""
    try:
        import contextlib
        if not page.get_drawings():
            return []  # a ruled table needs rules; skips the (slow) detector on plain pages
        with contextlib.redirect_stdout(io.StringIO()):
            tabs = page.find_tables()
    except Exception:
        return []
    out: list[Unit] = []
    for t in getattr(tabs, "tables", []):
        try:
            if t.row_count < 2 or t.col_count < 2:
                continue
            rows = t.extract()
        except Exception:
            continue
        if not rows or all(all(not (c or "").strip() for c in row) for row in rows):
            continue
        bbox = pymupdf.Rect(t.bbox)
        parts = ["<table>"]
        # PyMuPDF's header guess is unreliable (it happily takes a caption above
        # the table); a fully populated first row is a header far more often.
        first_is_header = len(rows) >= 2 and all((c or "").strip() for c in rows[0])
        for ri, row in enumerate(rows):
            tag = "th" if ri == 0 and first_is_header else "td"
            parts.append("<tr>" + "".join(
                f"<{tag}>{html.escape((c or '').strip())}</{tag}>" for c in row) + "</tr>")
        parts.append("</table>")
        out.append(Unit("table", bbox.x0, bbox.y0, bbox.x1, bbox.y1, pno, html="".join(parts)))
        for u in text_units:
            for r in u.runs:
                r.glyphs = [g for g in r.glyphs
                            if not (bbox.x0 - 1 <= (g.x0 + g.x1) / 2 <= bbox.x1 + 1 and bbox.y0 - 1 <= (g.y0 + g.y1) / 2 <= bbox.y1 + 1)]
                r.invalidate()
    return out


def _prune_empty(units: list[Unit]) -> list[Unit]:
    out = []
    for u in units:
        if u.kind == "text":
            u.runs = [r for r in u.runs if r.text.strip()]
            if not u.runs:
                continue
            _refit(u)
        out.append(u)
    return out


def _drop_running_heads(units: list[Unit], page: pymupdf.Page, an: Analysis) -> list[Unit]:
    prect = page.rect
    band = prect.height * BAND_FRACTION
    out = []
    for u in units:
        if u.kind == "text":
            in_top = u.y1 <= prect.y0 + band
            in_bottom = u.y0 >= prect.y1 - band
            if in_top or in_bottom:
                text = u.text.strip()
                where = "top" if in_top else "bottom"
                if (where, _band_signature(text)) in an.drop_signatures:
                    continue
                if _PAGE_NUMBER_RE.match(text) and u.nchars <= 12:
                    continue
                pos = (where, _pos_key(u.y0 if in_top else u.y1))
                if pos in an.drop_positions and (u.size < an.body_size * 0.92 or u.nchars <= 40):
                    continue
        out.append(u)
    return out


# --- Reading order (recursive XY-cut) ---------------------------------------
def _largest_gap_size(units: list[Unit], axis: str, min_gap: float) -> tuple[float, float] | None:
    """(gap width, cut position) of the widest empty stripe along *axis*."""
    ivs = sorted((u.x0, u.x1) if axis == "x" else (u.y0, u.y1) for u in units)
    merged: list[list[float]] = []
    for a, b in ivs:
        if merged and a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    best: tuple[float, float] | None = None
    for (a0, a1), (b0, b1) in zip(merged, merged[1:]):
        gap = b0 - a1
        if gap >= min_gap and (best is None or gap > best[0]):
            best = (gap, (a1 + b0) / 2)
    return best


def reading_order(units: list[Unit], vertical: bool, body_size: float) -> list[Unit]:
    """Recursive XY-cut.

    The widest empty stripe that no unit crosses wins, whichever axis it is
    on: a column gutter (wide) beats the inter-line gaps (narrow) so columns
    are read one after the other, while a full-width heading blocks the
    gutter and forces the top/bottom split first. Cuts along the cross axis
    (x for horizontal text) must be at least one em wide; cuts along the flow
    axis only 0.3 em, since those never change the order within a column.
    """
    if len(units) <= 1:
        return list(units)
    flow, cross = ("x", "y") if vertical else ("y", "x")
    cands = []
    g = _largest_gap_size(units, flow, body_size * 0.3)
    if g:
        cands.append((g[0], flow, g[1]))
    g = _largest_gap_size(units, cross, body_size * 1.0)
    if g:
        cands.append((g[0], cross, g[1]))
    for _gap, axis, cut in sorted(cands, reverse=True):
        if axis == "x":
            a = [u for u in units if (u.x0 + u.x1) / 2 < cut]
            b = [u for u in units if (u.x0 + u.x1) / 2 >= cut]
            first, second = (b, a) if vertical else (a, b)
        else:
            first = [u for u in units if (u.y0 + u.y1) / 2 < cut]
            second = [u for u in units if (u.y0 + u.y1) / 2 >= cut]
        if first and second:
            return reading_order(first, vertical, body_size) + reading_order(second, vertical, body_size)
    if vertical:
        return sorted(units, key=lambda u: (-u.x1, u.y0))
    return sorted(units, key=lambda u: (u.y0, u.x0))


def page_units(doc: pymupdf.Document, page: pymupdf.Page, pno: int, an: Analysis,
               seen_images: dict[bytes, ImageRef], img_counter: list[int],
               *, tables: bool = True, drawings: bool = True) -> list[Unit]:
    """Everything on one page as units in reading order."""
    d = _page_dict(page, raw=True)
    blocks = d["blocks"]
    units: list[Unit] = []
    page_chars = 0
    gutters = [] if an.vertical else _find_gutters(blocks, an.body_size)
    for b in blocks:
        if b["type"] != 0:
            continue
        runs = _block_to_runs(b, gutters)
        if not runs:
            continue
        for group in _cluster_runs(runs):
            u = Unit("text", 0, 0, 0, 0, pno, runs=group)
            _refit(u)
            page_chars += u.nchars
            units.append(u)

    units = _attach_ruby(units, an.body_size)
    _attach_links(units, page.get_links(), page)
    extra: list[Unit] = []
    table_units: list[Unit] = []
    if tables and not an.vertical:
        table_units = _table_units(page, units, pno)
        extra += table_units
    if drawings:
        extra += _drawing_units(page, units, pno, img_counter, exclude=table_units)
    extra += _image_units(doc, page, blocks, pno, page_chars, seen_images, img_counter)
    units = _prune_empty(units) + extra
    units = _drop_running_heads(units, page, an)
    return reading_order(units, an.vertical, an.body_size)


# --- Pass 2b: units → paragraphs → chapters ---------------------------------
@dataclass
class Para:
    kind: str                       # "p" | "h" | "image" | "table"
    page: int
    level: int = 0
    runs: list[Run] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    image: ImageRef | None = None
    html: str = ""
    last_full: bool = True          # did the last run reach the margin?
    size: float = 0.0

    @property
    def text(self) -> str:
        return _plain_text(self.runs)


@dataclass
class Chapter:
    name: str
    paras: list[Para] = field(default_factory=list)
    title: str = ""
    approx_bytes: int = 0


@dataclass
class ConvertResult:
    title: str
    language: str
    vertical: bool
    page_count: int
    chapter_count: int
    image_count: int
    notes: list[str] = field(default_factory=list)
    ocr_note: str | None = None


class _Margins:
    """Modal column edges for the runs of one page (indent / full-line tests)."""

    def __init__(self, page: pymupdf.Page, units: list[Unit]):
        self.rect = page.rect
        self.runs = [r for u in units if u.kind == "text" for r in u.runs]

    def _mode_edge(self, run: Run, edge: str) -> float | None:
        vals = []
        for r in self.runs:
            if r.vertical != run.vertical or abs(r.size - run.size) > run.size * 0.25:
                continue
            if run.vertical:
                if _overlap(r.y0, r.y1, run.y0, run.y1) <= 0:
                    continue
            elif _overlap(r.x0, r.x1, run.x0, run.x1) <= 0:
                continue
            vals.append(round(getattr(r, edge) / 2) * 2)
        if len(vals) < 3:
            return None
        return _mode(vals)

    def indented(self, run: Run) -> bool:
        edge = "y0" if run.vertical else "x0"
        m = self._mode_edge(run, edge)
        if m is None:
            return False
        d = getattr(run, edge) - m
        return run.size * 0.5 < d < run.size * 4

    def full(self, run: Run) -> bool:
        edge = "y1" if run.vertical else "x1"
        m = self._mode_edge(run, edge)
        if m is None:
            return True
        return m - getattr(run, edge) < run.size * 1.0

    def alignment(self, run: Run) -> str:
        if run.vertical:
            top, bottom = self._mode_edge(run, "y0"), self._mode_edge(run, "y1")
            if top is None or bottom is None:
                top, bottom = self.rect.y0 + 20, self.rect.y1 - 20
            gap_a, gap_b = run.y0 - top, bottom - run.y1
        else:
            left, right = self._mode_edge(run, "x0"), self._mode_edge(run, "x1")
            if left is None or right is None:
                left, right = self.rect.x0 + 20, self.rect.x1 - 20
            gap_a, gap_b = run.x0 - left, right - run.x1
        s = run.size
        width = (bottom - top) if run.vertical else (right - left)
        if gap_a > s and gap_b > s and abs(gap_a - gap_b) < max(s, 0.12 * max(width, 1)):
            return "center"
        if gap_b < s * 0.5 and gap_a > s * 3:
            return "right"
        return ""


def _plain_text(runs: list[Run]) -> str:
    parts: list[str] = []
    for r in runs:
        t = r.text.strip()
        if not t:
            continue
        if parts:
            parts.append(_joiner(parts[-1], t))
        parts.append(t)
    return "".join(parts)


def _no_space_script(ch: str) -> bool:
    """Scripts whose line breaks carry no space.

    Han, kana and CJK punctuation obviously; Hangul too, because renderers
    (Chromium included) break Korean between syllables by default, so a line
    end is more often mid-word than at a word space.
    """
    return is_cjk(ch)


def _joiner(prev: str, nxt: str) -> str:
    if not prev or not nxt:
        return ""
    a, b = prev[-1], nxt[0]
    if _no_space_script(a) or _no_space_script(b):
        return ""
    return " "


def _is_terminal(text: str) -> bool:
    t = text.rstrip()
    return bool(t) and t[-1] in _TERMINAL_PUNCT


def _heading_like(u: Unit, an: Analysis) -> bool:
    if u.kind != "text" or u.nchars == 0 or u.nchars > HEADING_MAX_CHARS or len(u.runs) > 4:
        return False
    size = u.size
    if size >= an.body_size * HEADING_RATIO:
        return True
    glyphs = [g for r in u.runs for g in r.glyphs if not _is_space(g.c)]
    all_bold = all(g.bold for g in glyphs)
    return (all_bold and len(u.runs) == 1 and u.nchars <= 60
            and not _is_terminal(u.text) and size >= an.body_size * 0.95)


_CJK_OPENERS = set("「『（【〈《")
_CJK_TERMINAL = set("。！？」』…")


def _new_paragraph_between(prev: Run, nxt: Run, margins: "_Margins") -> bool:
    """Does *nxt* start a new paragraph after *prev* inside one block?"""
    if margins.indented(nxt) and not margins.indented(prev):
        return True
    pt, nt = prev.text.rstrip(), nxt.text.lstrip()
    if pt and nt and nt[0] in _CJK_OPENERS and pt[-1] in _CJK_TERMINAL:
        return True
    return False


def _split_unit_paragraphs(u: Unit, margins: "_Margins") -> list[Unit]:
    pieces: list[Unit] = []
    cur: list[Run] = [u.runs[0]]
    for prev, nxt in zip(u.runs, u.runs[1:]):
        if _new_paragraph_between(prev, nxt, margins):
            pieces.append(Unit("text", 0, 0, 0, 0, u.page, runs=cur))
            cur = []
        cur.append(nxt)
    pieces.append(Unit("text", 0, 0, 0, 0, u.page, runs=cur))
    for p in pieces:
        _refit(p)
    return pieces


class Builder:
    """Second pass: walks the pages and accumulates chapters."""

    def __init__(self, doc: pymupdf.Document, an: Analysis, *,
                 tables: bool = True, drawings: bool = True,
                 progress_cb: Callable[[str], None] | None = None):
        self.doc = doc
        self.an = an
        self.tables = tables
        self.drawings = drawings
        self.progress_cb = progress_cb
        self.chapters: list[Chapter] = []
        self.images: dict[str, ImageRef] = {}
        self._seen_images: dict[bytes, ImageRef] = {}
        self._img_counter = [0]
        self._open: Para | None = None
        self._pending_ids: list[str] = []        # anchors waiting for the next unit
        self.page_anchor: dict[int, tuple[str, str]] = {}
        self.toc_anchor: dict[int, tuple[str, str]] = {}
        self.toc = doc.get_toc(simple=False)
        self._toc_by_page: dict[int, list[tuple[int, int, pymupdf.Point | None]]] = defaultdict(list)
        for i, entry in enumerate(self.toc):
            level, pno1 = entry[0], entry[2]
            dest = entry[3] if len(entry) > 3 else {}
            if pno1 is None or pno1 < 1:
                continue
            pt = dest.get("to") if isinstance(dest, dict) else None
            pt = pymupdf.Point(pt) if pt is not None else None
            self._toc_by_page[pno1 - 1].append((i, level, pt))
        self.indent_votes = Counter()

    # -- chapter management --
    def _chapter(self) -> Chapter:
        if not self.chapters:
            self._new_chapter()
        return self.chapters[-1]

    def _new_chapter(self) -> Chapter:
        ch = Chapter(name=f"ch{len(self.chapters) + 1:04d}.xhtml")
        self.chapters.append(ch)
        self._open = None
        return ch

    def _add(self, para: Para) -> None:
        ch = self._chapter()
        ch.paras.append(para)
        ch.approx_bytes += len(para.text) * 3 + 40
        for i in para.ids:
            if i.startswith("pg"):
                self.page_anchor[int(i[2:])] = (ch.name, i)
            elif i.startswith("toc"):
                self.toc_anchor[int(i[3:])] = (ch.name, i)
        self._open = para if para.kind == "p" else None

    # -- TOC targets --
    def _assign_toc_ids(self, units: list[Unit], pno: int, page: pymupdf.Page) -> dict[int, int]:
        """Return {id(unit): toc_level} for units that are bookmark targets."""
        levels: dict[int, int] = {}
        for idx, level, pt in self._toc_by_page.get(pno, []):
            target: Unit | None = None
            if pt is not None and units:
                if page.rotation:
                    pt = pt * page.rotation_matrix
                for u in units:
                    if u.x0 - 2 <= pt.x <= u.x1 + 2 and u.y0 - 2 <= pt.y <= u.y1 + 2:
                        target = u
                        break
                if target is None:
                    tol = self.an.body_size
                    for u in units:
                        if self.an.vertical:
                            if u.x0 <= pt.x + tol:
                                target = u
                                break
                        elif u.y1 >= pt.y - tol:
                            target = u
                            break
            if target is None and units:
                target = units[0]
            if target is None:
                self._pending_ids.append(f"toc{idx}")
                continue
            target.ids.append(f"toc{idx}")
            levels[id(target)] = min(levels.get(id(target), level), level)
        return levels

    # -- main loop --
    def run(self) -> None:
        doc = self.doc
        n = doc.page_count
        for pno in range(n):
            page = doc[pno]
            if self.progress_cb and (pno % 5 == 0 or pno == n - 1):
                self.progress_cb(f"Building ePUB (page {pno + 1}/{n})")
            try:
                units = page_units(doc, page, pno, self.an, self._seen_images, self._img_counter,
                                   tables=self.tables, drawings=self.drawings)
            except Exception as e:  # a broken page must not kill the book
                print(f"[pdf2epub] page {pno + 1}: {e}")
                units = []
            toc_levels = self._assign_toc_ids(units, pno, page)
            margins = _Margins(page, units)
            first_on_page = True
            if not units:
                self._pending_ids.append(f"pg{pno}")
                continue
            for u in units:
                ids = list(u.ids)
                if first_on_page:
                    ids.insert(0, f"pg{pno}")
                if self._pending_ids:
                    ids = self._pending_ids + ids
                    self._pending_ids = []
                first_on_page = False
                self._consume(u, ids, toc_levels.get(id(u)), margins, pno)

    def _consume(self, u: Unit, ids: list[str], toc_level: int | None,
                 margins: _Margins, pno: int) -> None:
        if u.kind == "text" and len(u.runs) > 1 and not _heading_like(u, self.an):
            pieces = _split_unit_paragraphs(u, margins)
            if len(pieces) > 1:
                for i, piece in enumerate(pieces):
                    self._consume_one(piece, ids if i == 0 else [], toc_level if i == 0 else None, margins, pno)
                return
        self._consume_one(u, ids, toc_level, margins, pno)

    def _consume_one(self, u: Unit, ids: list[str], toc_level: int | None,
                     margins: _Margins, pno: int) -> None:
        if u.kind == "image" and u.image is not None:
            self.images[u.image.name] = u.image
            self._add(Para("image", pno, ids=ids, image=u.image))
            return
        if u.kind == "table":
            self._add(Para("table", pno, ids=ids, html=u.html))
            return

        size = u.size
        first, last = u.runs[0], u.runs[-1]
        indented = margins.indented(first)
        full = margins.full(last)
        align = margins.alignment(first) if len(u.runs) == 1 else ""
        heading = _heading_like(u, self.an) or (toc_level is not None and u.nchars <= HEADING_MAX_CHARS and len(u.runs) <= 4)

        # A level-1 bookmark (or, without bookmarks, an h1) opens a new file.
        starts_chapter = False
        if heading:
            level = toc_level if toc_level is not None else self.an.heading_level(size)
            if size < self.an.body_size * HEADING_RATIO and toc_level is None:
                level = min(len(self.an.heading_sizes) + 1, 4)
            level = max(1, min(level, 4))
            starts_chapter = (level == 1) and (toc_level is not None or not self.an.has_bookmarks)
        if not starts_chapter and any(i.startswith("toc") for i in ids) and toc_level == 1:
            starts_chapter = True
        chapter = self._chapter()
        if chapter.paras and (starts_chapter or (chapter.approx_bytes > MAX_FILE_BYTES and ids and ids[0].startswith("pg"))):
            self._new_chapter()

        if heading:
            para = Para("h", pno, level=level, runs=list(u.runs), ids=ids, size=size)
            if align:
                para.classes.append(align)
            self._add(para)
            return

        open_ = self._open
        can_merge = (
            open_ is not None and open_.kind == "p" and not ids[1:] and not any(i.startswith("toc") for i in ids)
            and "center" not in open_.classes and "right" not in open_.classes and not align
            and not indented and open_.last_full
            and abs(open_.size - size) <= max(open_.size, size) * 0.15
            and open_.runs and open_.runs[-1].vertical == first.vertical
        )
        if can_merge:
            pt, nt = open_.runs[-1].text.rstrip(), first.text.lstrip()
            if pt and nt and nt[0] in _CJK_OPENERS and pt[-1] in _CJK_TERMINAL:
                can_merge = False
        if can_merge and open_.page == pno:
            prev = open_.runs[-1]
            gap = (prev.x0 - u.x1) if first.vertical else (u.y0 - prev.y1)
            if gap > size * (1.4 if first.vertical else 1.2):
                can_merge = False
        if can_merge:
            # page anchors ride along on the merged paragraph
            open_.ids.extend(ids)
            for i in ids:
                self.page_anchor[int(i[2:])] = (self._chapter().name, i)
            open_.runs.extend(u.runs)
            open_.last_full = full
            open_.page = pno
            self._chapter().approx_bytes += u.nchars * 3
            return

        para = Para("p", pno, runs=list(u.runs), ids=ids, size=size, last_full=full)
        if align:
            para.classes.append(align)
        elif indented:
            para.classes.append("indent")
            self.indent_votes["indent"] += 1
        else:
            para.classes.append("noindent")
            self.indent_votes["noindent"] += 1
        if size and size <= self.an.body_size * SMALL_RATIO:
            para.classes.append("small")
        self._add(para)


# --- HTML emission ----------------------------------------------------------
def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _merge_runs_to_glyphs(runs: list[Run]) -> list[tuple[Glyph, int]]:
    """Flatten runs into (glyph, run_index) with joiners and dehyphenation."""
    out: list[tuple[Glyph, int]] = []
    for ri, r in enumerate(runs):
        glyphs = list(r.glyphs)
        while glyphs and _is_space(glyphs[0].c):
            glyphs.pop(0)
        while glyphs and _is_space(glyphs[-1].c):
            glyphs.pop()
        if not glyphs:
            continue
        if out:
            prev = out[-1][0]
            nxt = glyphs[0]
            if (not r.vertical and prev.c in _HYPHENS and nxt.c.isalpha()
                    and len(out) >= 2 and out[-2][0].c.isalpha()):
                # A word broken at a line end. Lower-case continuation: the
                # hyphen was inserted by hyphenation, drop it ("commu-nication").
                # Upper-case continuation: a real compound ("PARAGRAPH-END"),
                # keep the hyphen but never add a space.
                if nxt.c.islower():
                    out.pop()
            elif not (_no_space_script(prev.c) or _no_space_script(nxt.c)):
                sp = Glyph(" ", prev.x1, prev.y0, prev.x1, prev.y1, prev.size,
                           prev.bold and nxt.bold, prev.italic and nxt.italic, False, False,
                           prev.link if prev.link == nxt.link else None)
                out.append((sp, out[-1][1]))
        out.extend((g, ri) for g in glyphs)
    return out


def _inline_html(runs: list[Run], *, in_heading: bool = False) -> str:
    items = _merge_runs_to_glyphs(runs)
    if not items:
        return ""
    glyphs = [g for g, _ in items]
    drop_bold = in_heading or all(g.bold for g in glyphs if not _is_space(g.c))
    drop_italic = in_heading and all(g.italic for g in glyphs if not _is_space(g.c))

    out: list[str] = []
    state: tuple | None = None
    open_tags: list[str] = []

    def close_all() -> None:
        while open_tags:
            out.append(f"</{open_tags.pop()}>")

    def set_state(g: Glyph) -> None:
        nonlocal state
        s = (g.link, g.bold and not drop_bold, g.italic and not drop_italic, g.sup, g.mono)
        if s == state:
            return
        close_all()
        state = s
        link, bold, italic, sup, mono = s
        if link:
            out.append(f'<a href="{html.escape(link, quote=True)}">')
            open_tags.append("a")
        if bold:
            out.append("<strong>")
            open_tags.append("strong")
        if italic:
            out.append("<em>")
            open_tags.append("em")
        if sup:
            out.append("<sup>")
            open_tags.append("sup")
        if mono:
            out.append("<code>")
            open_tags.append("code")

    i = 0
    n = len(items)
    while i < n:
        g, ri = items[i]
        if g.ruby >= 0:
            rid = g.ruby
            j = i
            while j < n and items[j][1] == ri and items[j][0].ruby == rid:
                j += 1
            set_state(g)
            base = "".join(_esc(x.c) for x, _ in items[i:j])
            rt = _esc(runs[ri].rubies[rid])
            out.append(f"<ruby>{base}<rt>{rt}</rt></ruby>")
            i = j
            continue
        set_state(g)
        out.append(_esc(g.c))
        i += 1
    close_all()
    text = "".join(out)
    return re.sub(r"[ \t]{2,}", " ", text)


def _para_html(p: Para, all_indent: bool) -> str:
    idattr = f' id="{p.ids[0]}"' if p.ids else ""
    anchors = "".join(f'<span id="{i}"></span>' for i in p.ids[1:])
    if p.kind == "image" and p.image is not None:
        w = p.image.width
        return (f'<div class="figure"{idattr}>{anchors}<img src="../images/{p.image.name}" alt=""'
                + (f' width="{w}"' if 0 < w < 300 else "") + "/></div>")
    if p.kind == "table":
        return f'<div class="tablewrap"{idattr}>{anchors}{p.html}</div>'
    if p.kind == "h":
        cls = f' class="{" ".join(p.classes)}"' if p.classes else ""
        return f"<h{p.level}{idattr}{cls}>{anchors}{_inline_html(p.runs, in_heading=True)}</h{p.level}>"
    classes = [c for c in p.classes if not (all_indent and c == "indent") and not (not all_indent and c == "noindent")]
    cls = f' class="{" ".join(classes)}"' if classes else ""
    body = _inline_html(p.runs)
    if not body.strip():
        return anchors and f"<p{idattr}>{anchors}</p>" or ""
    return f"<p{idattr}{cls}>{anchors}{body}</p>"


_XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
    'xml:lang="{lang}" lang="{lang}">\n<head>\n<meta charset="utf-8"/>\n<title>{title}</title>\n'
    '<link rel="stylesheet" type="text/css" href="{css}"/>\n</head>\n<body{bodycls}>\n'
)
_XHTML_FOOT = "</body>\n</html>\n"


def _css(vertical: bool, lang: str, all_indent: bool) -> str:
    indent = "2em" if lang.startswith("zh") else "1em"
    css = [
        "html, body { margin: 0; padding: 0; }",
        "body { line-height: 1.75; }",
        "h1, h2, h3, h4 { font-weight: bold; line-height: 1.3; }",
        "h1 { font-size: 1.6em; } h2 { font-size: 1.35em; } h3 { font-size: 1.15em; } h4 { font-size: 1em; }",
        f"p {{ margin: 0; text-indent: {indent}; }}" if all_indent else "p { margin: 0 0 0.8em 0; text-indent: 0; }",
        f"p.indent {{ text-indent: {indent}; }}",
        "p.noindent { text-indent: 0; }",
        "p.center, h1.center, h2.center, h3.center { text-align: center; text-indent: 0; }",
        "p.right { text-align: right; text-indent: 0; }",
        "p.small { font-size: 0.85em; }",
        ".figure { text-align: center; text-indent: 0; margin: 1em 0; }",
        ".figure img { max-width: 100%; height: auto; }",
        ".tablewrap { margin: 1em 0; }",
        "table { border-collapse: collapse; font-size: 0.9em; }",
        "td, th { border: 1px solid #999; padding: 0.2em 0.5em; vertical-align: top; }",
        "ruby { ruby-align: center; -webkit-ruby-position: over; ruby-position: over; }",
        "rt { font-size: 0.5em; line-height: 1; }",
        "sup { font-size: 0.7em; vertical-align: super; line-height: 0; }",
        "a { text-decoration: underline; }",
    ]
    if vertical:
        css += [
            "html { writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl; -epub-writing-mode: vertical-rl;"
            " text-orientation: mixed; -webkit-text-orientation: mixed; -epub-text-orientation: mixed; }",
            "h1, h2, h3, h4 { margin: 0 0.5em 0 1em; }",
            "p { margin: 0; }",
            ".figure { margin: 0 1em; text-align: center; }",
            ".figure img { max-height: 100%; max-width: 100%; width: auto; height: auto; }",
            ".tablewrap { margin: 0 1em; }",
            "rt { -webkit-ruby-position: over; ruby-position: over; }",
            "section.cover { writing-mode: horizontal-tb; -webkit-writing-mode: horizontal-tb; -epub-writing-mode: horizontal-tb; }",
        ]
    css.append("section.cover { text-align: center; margin: 0; padding: 0; }")
    css.append("section.cover img { max-width: 100%; max-height: 100%; }")
    return "\n".join(css) + "\n"


def _nav_tree(entries: list[tuple[int, str, str]]) -> str:
    """entries: (level, title, href) → nested <ol> for nav.xhtml."""
    if not entries:
        return "<ol></ol>"
    out = ["<ol>"]
    depth = 1
    prev_level = 1
    first = True
    for level, title, href in entries:
        level = max(1, level)
        if first:
            level = 1
        elif level > prev_level:
            level = prev_level + 1
            for _ in range(level - prev_level):
                out.append("<ol>")
                depth += 1
        elif level < prev_level:
            for _ in range(prev_level - level):
                out.append("</li></ol>")
                depth -= 1
            out.append("</li>")
        else:
            out.append("</li>")
        out.append(f'<li><a href="{html.escape(href, quote=True)}">{_esc(title) or "—"}</a>')
        prev_level = level
        first = False
    out.append("</li>")
    while depth > 1:
        out.append("</ol></li>")
        depth -= 1
    out.append("</ol>")
    return "".join(out)


def _ncx_tree(entries: list[tuple[int, str, str]]) -> str:
    out: list[str] = []
    stack: list[int] = []
    order = 0
    prev_level = 0
    for level, title, href in entries:
        level = max(1, level)
        if order == 0:
            level = 1
        elif level > prev_level:
            level = prev_level + 1
        while stack and stack[-1] >= level:
            out.append("</navPoint>")
            stack.pop()
        order += 1
        out.append(f'<navPoint id="np{order}" playOrder="{order}"><navLabel><text>{_esc(title) or "—"}</text></navLabel>'
                   f'<content src="{html.escape(href, quote=True)}"/>')
        stack.append(level)
        prev_level = level
    while stack:
        out.append("</navPoint>")
        stack.pop()
    return "".join(out)


def _resolve_page_links(text: str, this_file: str, page_anchor: dict[int, tuple[str, str]]) -> str:
    def repl(m: re.Match) -> str:
        pno = int(m.group(1))
        target = page_anchor.get(pno)
        if target is None:
            # nearest following page with an anchor, else drop the link target
            later = [p for p in page_anchor if p > pno]
            if not later:
                return 'href="#"'
            target = page_anchor[min(later)]
        f, i = target
        return f'href="#{i}"' if f == this_file else f'href="{f}#{i}"'
    return re.sub(r'href="#__page_(\d+)"', repl, text)


def write_epub(
    out_path: Path,
    *,
    builder: Builder,
    title: str,
    author: str,
    language: str,
    vertical: bool,
    cover: ImageRef | None,
) -> None:
    all_indent = builder.indent_votes["indent"] > builder.indent_votes["noindent"]
    chapters = [c for c in builder.chapters if c.paras] or [Chapter("ch0001.xhtml")]
    for i, ch in enumerate(chapters):
        heads = [p for p in ch.paras if p.kind == "h"]
        ch.title = heads[0].text if heads else (title if i == 0 else f"Section {i + 1}")

    # Navigation entries from bookmarks, else from headings, else one per file.
    entries: list[tuple[int, str, str]] = []
    if builder.toc:
        for idx, entry in enumerate(builder.toc):
            level, ttl, pno1 = entry[0], entry[1], entry[2]
            target = builder.toc_anchor.get(idx)
            if target is None and pno1 and pno1 >= 1:
                target = builder.page_anchor.get(pno1 - 1)
                if target is None:
                    later = sorted(p for p in builder.page_anchor if p >= pno1 - 1)
                    target = builder.page_anchor[later[0]] if later else None
            if target is None:
                target = (chapters[0].name, "")
            f, i = target
            entries.append((level, (ttl or "").strip(), f"text/{f}" + (f"#{i}" if i else "")))
    else:
        for ch in chapters:
            for p in ch.paras:
                if p.kind == "h" and p.level <= 2 and p.ids:
                    entries.append((p.level, p.text, f"text/{ch.name}#{p.ids[0]}"))
        if not entries:
            entries = [(1, ch.title, f"text/{ch.name}") for ch in chapters]

    lang = language or "en"
    uid = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
            '</container>\n'), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/styles.css", _css(vertical, lang, all_indent), compress_type=zipfile.ZIP_DEFLATED)

        manifest = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="css" href="styles.css" media-type="text/css"/>',
        ]
        spine: list[str] = []

        if cover is not None:
            zf.writestr(f"OEBPS/images/{cover.name}", cover.data, compress_type=zipfile.ZIP_STORED)
            manifest.append(f'<item id="cover-image" href="images/{cover.name}" media-type="{cover.media_type}" properties="cover-image"/>')
            manifest.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
            spine.append('<itemref idref="cover" linear="yes"/>')
            zf.writestr("OEBPS/cover.xhtml",
                        _XHTML_HEAD.format(lang=lang, title=_esc(title), css="styles.css", bodycls="")
                        + f'<section class="cover" epub:type="cover"><img src="images/{cover.name}" alt="{_esc(title)}"/></section>\n'
                        + _XHTML_FOOT, compress_type=zipfile.ZIP_DEFLATED)

        for name, ref in builder.images.items():
            zf.writestr(f"OEBPS/images/{name}", ref.data, compress_type=zipfile.ZIP_STORED)
            mid = "img_" + re.sub(r"[^A-Za-z0-9]", "_", name)
            manifest.append(f'<item id="{mid}" href="images/{name}" media-type="{ref.media_type}"/>')

        for i, ch in enumerate(chapters):
            body = "\n".join(h for h in (_para_html(p, all_indent) for p in ch.paras) if h)
            body = _resolve_page_links(body, ch.name, builder.page_anchor)
            doc = (_XHTML_HEAD.format(lang=lang, title=_esc(ch.title or title), css="../styles.css", bodycls="")
                   + f'<section epub:type="{"chapter" if builder.toc or len(chapters) > 1 else "bodymatter"}">\n'
                   + body + "\n</section>\n" + _XHTML_FOOT)
            zf.writestr(f"OEBPS/text/{ch.name}", doc, compress_type=zipfile.ZIP_DEFLATED)
            cid = f"c{i + 1}"
            manifest.append(f'<item id="{cid}" href="text/{ch.name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{cid}"/>')

        nav = (_XHTML_HEAD.format(lang=lang, title=_esc(title), css="styles.css", bodycls="")
               + '<nav epub:type="toc" id="toc"><h1>' + _esc(title) + '</h1>\n' + _nav_tree(entries) + '\n</nav>\n'
               + _XHTML_FOOT)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)

        ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'<head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="{max((e[0] for e in entries), default=1)}"/>'
            '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
            f'<docTitle><text>{_esc(title)}</text></docTitle>\n<navMap>' + _ncx_tree(entries) + '</navMap>\n</ncx>\n'
        )
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)

        meta = [
            f'<dc:identifier id="bookid">{uid}</dc:identifier>',
            f'<dc:title>{_esc(title)}</dc:title>',
            f'<dc:language>{_esc(lang)}</dc:language>',
            f'<meta property="dcterms:modified">{modified}</meta>',
        ]
        if author:
            meta.append(f'<dc:creator>{_esc(author)}</dc:creator>')
        if cover is not None:
            meta.append('<meta name="cover" content="cover-image"/>')
        if vertical:
            meta.append('<meta name="primary-writing-mode" content="vertical-rl"/>')
        ppd = ' page-progression-direction="rtl"' if vertical else ""
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
            f'xml:lang="{_esc(lang)}">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' + "\n".join(meta) + '\n</metadata>\n'
            '<manifest>\n' + "\n".join(manifest) + '\n</manifest>\n'
            f'<spine toc="ncx"{ppd}>\n' + "\n".join(spine) + '\n</spine>\n</package>\n'
        )
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)


# --- Top level --------------------------------------------------------------
def resolve_language(catalog_lang: str | None, guessed: str) -> str:
    """Prefer the PDF's declared language, refined by script statistics."""
    if not catalog_lang:
        return guessed
    tag = catalog_lang.replace("_", "-")
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", tag):
        return guessed
    low = tag.lower()
    # A bare "zh" is refined to the script we actually saw.
    if low == "zh" and guessed.startswith("zh-"):
        return guessed
    # Trust the script evidence over a plainly wrong declaration (e.g. "en"
    # on a Japanese book – common with careless producers).
    if low.startswith("en") and guessed != "en":
        return guessed
    return tag


def convert(
    pdf_path: Path,
    out_epub: Path,
    *,
    title: str | None = None,
    author: str | None = None,
    progress_cb: Callable[[str], None] | None = None,
    ocr_mode: str = "off",
    ocr_langs: str = "eng",
    ocr_jobs: int = 2,
    pua_threshold: float = 0.20,
    tables: bool = True,
    drawings: bool = True,
) -> ConvertResult:
    """Convert *pdf_path* to an EPUB at *out_epub*. Raises PdfError on failure.

    When *ocr_mode* is "auto", scanned PDFs and PUA-obfuscated text layers are
    repaired with ocrmypdf first (the PDF is modified in place, so pass a copy).
    """
    validate(pdf_path)
    an = analyze(pdf_path, progress_cb)
    an, ocr_note = prepare_text_layer(
        pdf_path, an, mode=ocr_mode, langs=ocr_langs, jobs=ocr_jobs,
        pua_threshold=pua_threshold, progress_cb=progress_cb,
    )

    doc = pymupdf.open(str(pdf_path))
    try:
        meta = doc.metadata or {}
        title = (title or "").strip() or _clean_title(meta.get("title"), pdf_path.stem)
        author = (author if author is not None else (meta.get("author") or "")).strip()
        language = resolve_language(_catalog_lang(doc), an.language)

        builder = Builder(doc, an, tables=tables, drawings=drawings, progress_cb=progress_cb)
        builder.run()

        if progress_cb:
            progress_cb("Packaging ePUB")
        try:
            cover = cover_image(doc)
        except Exception:
            cover = None
        # Don't ship page 1 twice: if it was a full-page image and page 1's
        # only content, that image already is the cover.
        first_paras = builder.chapters[0].paras[:1] if builder.chapters and builder.chapters[0].paras else []
        if cover and first_paras and first_paras[0].kind == "image" and first_paras[0].page == 0 \
                and first_paras[0].image is not None and cover.data == first_paras[0].image.data:
            builder.chapters[0].paras.pop(0)

        write_epub(out_epub, builder=builder, title=title, author=author,
                   language=language, vertical=an.vertical, cover=cover)
        notes: list[str] = []
        if an.text_coverage < SCANNED_TEXT_COVERAGE and an.page_count:
            notes.append("little or no text layer; pages exported as images")
        return ConvertResult(
            title=title, language=language, vertical=an.vertical,
            page_count=doc.page_count,
            chapter_count=sum(1 for c in builder.chapters if c.paras),
            image_count=len(builder.images), notes=notes, ocr_note=ocr_note,
        )
    finally:
        doc.close()
