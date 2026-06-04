"""ePUB inspection and PDF conversion.

This module is deliberately dependency-light: ePUB structure is parsed with the
standard-library zipfile + xml.etree, and only Pillow is used (for cover
thumbnailing). The actual PDF rendering is delegated to the Vivliostyle CLI,
invoked as a subprocess.
"""
from __future__ import annotations

import copy
import functools
import io
import posixpath
import re
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:  # pragma: no cover - Pillow should be installed
    _HAS_PIL = False

# Register common OPF namespaces so ET.tostring preserves prefixes.
ET.register_namespace("", "http://www.idpf.org/2007/opf")
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
ET.register_namespace("dcterms", "http://purl.org/dc/terms/")


# Algorithms used purely for font obfuscation (NOT content DRM). An ePUB whose
# encryption.xml references only these is still readable.
_FONT_OBFUSCATION_ALGOS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

# Keywords in EpubError messages that indicate non-transient (structural)
# errors which should NOT be retried.
_NON_RETRYABLE_KEYWORDS = ("drm", "not a valid", "malformed", "not found")

# Base delay (seconds) for exponential backoff between retry attempts.
_RETRY_BACKOFF_BASE_SEC = 5

_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


class EpubError(Exception):
    """Raised when an ePUB is invalid, encrypted, or otherwise unusable."""


@dataclass
class EpubInfo:
    title: str
    fixed_layout: bool = False
    page_direction: str | None = None  # "rtl" | "ltr" | None
    epub_language: str | None = None   # raw BCP 47 tag from dc:language, e.g. "zh-TW"
    cover_bytes: bytes | None = None
    cover_ext: str | None = None
    warnings: list[str] = field(default_factory=list)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter(elem: ET.Element, name: str):
    """Yield descendants whose local tag name matches, ignoring namespaces."""
    for e in elem.iter():
        if _localname(e.tag) == name:
            yield e


# --- Validation -------------------------------------------------------------
def validate(epub_path: Path) -> None:
    """Validate basic ePUB structure and reject DRM-protected files."""
    if not zipfile.is_zipfile(epub_path):
        raise EpubError("This file is not a valid ePUB (not a ZIP archive).")

    with zipfile.ZipFile(epub_path) as zf:
        names = set(zf.namelist())

        # The mimetype entry, when present, must declare an ePUB.
        if "mimetype" in names:
            mt = zf.read("mimetype").decode("ascii", "ignore").strip()
            if mt and mt != "application/epub+zip":
                raise EpubError(f"Unexpected mimetype: {mt!r}. Not an ePUB.")

        if "META-INF/container.xml" not in names:
            raise EpubError("Malformed ePUB: missing META-INF/container.xml.")

        # DRM detection via encryption.xml.
        if "META-INF/encryption.xml" in names:
            _check_encryption(zf.read("META-INF/encryption.xml"))


def _check_encryption(data: bytes) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Unparseable encryption manifest -> treat conservatively as DRM.
        raise EpubError("This ePUB appears to be DRM-protected (cannot convert).")

    algos = {
        e.get("Algorithm", "")
        for e in _iter(root, "EncryptionMethod")
        if e.get("Algorithm")
    }
    # If any encrypted resource uses something other than font obfuscation,
    # it is content DRM.
    if any(a not in _FONT_OBFUSCATION_ALGOS for a in algos):
        raise EpubError("This ePUB is DRM-protected and cannot be converted.")


# --- Metadata & cover -------------------------------------------------------
def extract_info(epub_path: Path) -> EpubInfo:
    """Read title, layout mode, reading direction, and cover image."""
    with zipfile.ZipFile(epub_path) as zf:
        opf_path = _find_opf_path(zf)
        opf_root = ET.fromstring(zf.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)

        title = _read_title(opf_root) or epub_path.stem
        fixed_layout = _read_fixed_layout(opf_root)
        page_direction = _read_page_direction(opf_root)
        epub_language = _read_language(opf_root)
        cover_href = _find_cover_href(opf_root)

        info = EpubInfo(
            title=title,
            fixed_layout=fixed_layout,
            page_direction=page_direction,
            epub_language=epub_language,
        )

        if cover_href:
            cover_zip_path = posixpath.normpath(posixpath.join(opf_dir, cover_href))
            try:
                raw = zf.read(cover_zip_path)
                info.cover_bytes, info.cover_ext = _thumbnail(raw, cover_href)
            except KeyError:
                info.warnings.append("Cover referenced but not found in archive.")
        return info


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    for rootfile in _iter(container, "rootfile"):
        full = rootfile.get("full-path")
        if full:
            return full
    raise EpubError("Malformed ePUB: no rootfile in container.xml.")


def _read_title(opf_root: ET.Element) -> str | None:
    for t in _iter(opf_root, "title"):
        if t.text and t.text.strip():
            return t.text.strip()
    return None


def _read_fixed_layout(opf_root: ET.Element) -> bool:
    for meta in _iter(opf_root, "meta"):
        if meta.get("property") == "rendition:layout":
            if (meta.text or "").strip() == "pre-paginated":
                return True
    return False


def _read_page_direction(opf_root: ET.Element) -> str | None:
    for spine in _iter(opf_root, "spine"):
        d = spine.get("page-progression-direction")
        if d in ("rtl", "ltr"):
            return d
    return None


def _read_language(opf_root: ET.Element) -> str | None:
    """Return the first dc:language value from the OPF, or None."""
    for lang in _iter(opf_root, "language"):
        val = (lang.text or "").strip()
        if val:
            return val
    return None


def _find_cover_href(opf_root: ET.Element) -> str | None:
    items = list(_iter(opf_root, "item"))

    # 1. EPUB3: manifest item with properties="cover-image".
    for it in items:
        props = (it.get("properties") or "").split()
        if "cover-image" in props and it.get("href"):
            return it.get("href")

    # 2. EPUB2: <meta name="cover" content="ID"> -> item with that id.
    cover_id = None
    for meta in _iter(opf_root, "meta"):
        if meta.get("name") == "cover":
            cover_id = meta.get("content")
            break
    if cover_id:
        for it in items:
            if it.get("id") == cover_id and it.get("href"):
                return it.get("href")

    # 3. Fallback: any image item whose id/href hints at a cover.
    for it in items:
        mt = it.get("media-type", "")
        href = it.get("href", "")
        if mt.startswith("image/") and "cover" in (it.get("id", "") + href).lower():
            return href
    return None


def _thumbnail(raw: bytes, href: str) -> tuple[bytes, str]:
    """Return (bytes, extension) for a cover, downscaled when possible."""
    from config import COVER_THUMB_WIDTH

    ext = posixpath.splitext(href)[1].lower() or ".img"
    # SVG (or no Pillow) -> store as-is.
    if ext == ".svg" or not _HAS_PIL:
        return raw, ext

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.width > COVER_THUMB_WIDTH:
            ratio = COVER_THUMB_WIDTH / img.width
            img = img.resize(
                (COVER_THUMB_WIDTH, max(1, int(img.height * ratio))),
                Image.LANCZOS,
            )
        out = io.BytesIO()
        fmt = "PNG" if img.mode in ("RGBA", "P", "LA") else "JPEG"
        if fmt == "JPEG":
            img = img.convert("RGB")
        img.save(out, format=fmt, quality=82)
        return out.getvalue(), ".png" if fmt == "PNG" else ".jpg"
    except Exception:
        # Unreadable image -> keep the original bytes.
        return raw, ext


# --- Rendering --------------------------------------------------------------
def build_vivliostyle_cmd(
    epub_path: Path,
    out_pdf: Path,
    *,
    size: str,
    fixed_layout: bool,
    page_direction: str | None,
    timeout: int,
    chromium_path: str | None,
) -> list[str]:
    """Construct the Vivliostyle CLI argument list (pure, for testability)."""
    cmd = [
        "vivliostyle", "build", str(epub_path),
        "-o", str(out_pdf),
        "--timeout", str(timeout),
    ]
    if chromium_path:
        cmd += ["--executable-browser", chromium_path]
    # Fixed-layout books carry their own page size; only override for reflowable.
    if not fixed_layout and size:
        cmd += ["-s", size]
    if page_direction in ("rtl", "ltr"):
        cmd += ["--reading-progression", page_direction]
    return cmd


def render_pdf(
    epub_path: Path,
    out_pdf: Path,
    info: EpubInfo,
    *,
    size: str,
    timeout: int,
    chromium_path: str | None,
    cwd: Path | None = None,
) -> None:
    """Invoke Vivliostyle to produce a PDF. Raises EpubError on failure."""
    cmd = build_vivliostyle_cmd(
        epub_path, out_pdf,
        size=size,
        fixed_layout=info.fixed_layout,
        page_direction=info.page_direction,
        timeout=timeout,
        chromium_path=chromium_path,
    )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        raise EpubError(
            f"Rendering timed out after {timeout}s. The book may be very large; "
            "try raising JOB_TIMEOUT_SEC."
        )
    except FileNotFoundError:
        raise EpubError(
            "The Vivliostyle CLI was not found. Is it installed in the container?"
        )

    if proc.returncode != 0 or not out_pdf.exists() or out_pdf.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        detail = "\n".join(tail) if tail else "no output"
        raise EpubError(f"Vivliostyle failed to render the PDF.\n{detail}")


# --- Chunked rendering ------------------------------------------------------

def extract_spine_idrefs(epub_path: Path) -> tuple[str, ET.Element, list[str]]:
    """Return (opf_path, opf_root, ordered list of spine itemref idrefs)."""
    with zipfile.ZipFile(epub_path) as zf:
        opf_path = _find_opf_path(zf)
        opf_root = ET.fromstring(zf.read(opf_path))

    idrefs: list[str] = []
    for spine in _iter(opf_root, "spine"):
        for child in spine:
            if _localname(child.tag) == "itemref":
                idref = child.get("idref")
                if idref:
                    idrefs.append(idref)
        break  # only the first <spine>
    return opf_path, opf_root, idrefs


def estimate_chunk_timeout(num_items: int) -> int:
    """Calculate an adaptive timeout (seconds) for a chunk of spine items."""
    from config import ADAPTIVE_TIMEOUT_BASE, ADAPTIVE_TIMEOUT_PER_SPINE_ITEM
    return ADAPTIVE_TIMEOUT_BASE + num_items * ADAPTIVE_TIMEOUT_PER_SPINE_ITEM


def _create_chunk_epub(
    epub_path: Path,
    opf_path: str,
    opf_root: ET.Element,
    chunk_idrefs: set[str],
    out_epub: Path,
) -> None:
    """Create a sub-EPUB whose spine contains only *chunk_idrefs*.

    All manifest items (CSS, images, fonts) are preserved so every chapter
    can still reference its resources.
    """
    chunk_opf = copy.deepcopy(opf_root)
    for spine in _iter(chunk_opf, "spine"):
        for child in list(spine):
            if _localname(child.tag) == "itemref":
                if child.get("idref") not in chunk_idrefs:
                    spine.remove(child)
        break

    modified_opf = ET.tostring(chunk_opf, encoding="utf-8",
                               xml_declaration=True)

    with zipfile.ZipFile(epub_path, "r") as src, \
         zipfile.ZipFile(out_epub, "w") as dst:
        # mimetype must be first entry, stored uncompressed (EPUB spec).
        for item in src.infolist():
            if item.filename == "mimetype":
                dst.writestr(item, src.read(item.filename),
                             compress_type=zipfile.ZIP_STORED)
                break

        for item in src.infolist():
            if item.filename == "mimetype":
                continue
            if item.filename == opf_path:
                dst.writestr(item.filename, modified_opf,
                             compress_type=zipfile.ZIP_DEFLATED)
            else:
                dst.writestr(item, src.read(item.filename))


def _render_with_retry(
    epub_path: Path,
    out_pdf: Path,
    info: EpubInfo,
    *,
    size: str,
    timeout: int,
    chromium_path: str | None,
    cwd: Path | None = None,
    max_retries: int = 2,
) -> None:
    """Render a single PDF with exponential-backoff retry on transient errors.

    The initial attempt always runs. If it fails with a transient error, up to
    *max_retries* additional attempts are made (so total attempts = 1 + max_retries).
    """
    last_err: EpubError | None = None
    total_attempts = 1 + max(0, max_retries)
    for attempt in range(1, total_attempts + 1):
        try:
            # Scale timeout upward on retries.
            attempt_timeout = timeout * attempt
            render_pdf(
                epub_path, out_pdf, info,
                size=size, timeout=attempt_timeout,
                chromium_path=chromium_path, cwd=cwd,
            )
            return  # success
        except EpubError as e:
            last_err = e
            # Non-transient errors should not be retried.
            msg = str(e).lower()
            if any(kw in msg for kw in _NON_RETRYABLE_KEYWORDS):
                raise
            if attempt < total_attempts:
                time.sleep(_RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    if last_err is None:
        raise EpubError("Rendering failed (no error details captured).")
    raise last_err


def merge_pdfs(pdf_paths: list[Path], out_path: Path) -> None:
    """Merge multiple PDFs into a single file using pypdf."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    try:
        for p in pdf_paths:
            writer.append(str(p))
        writer.write(str(out_path))
    finally:
        writer.close()


def render_pdf_chunked(
    epub_path: Path,
    out_pdf: Path,
    info: EpubInfo,
    *,
    size: str,
    base_timeout: int,
    chromium_path: str | None,
    cwd: Path | None = None,
    chunk_size: int,
    max_retries: int,
    progress_cb: Callable[[str], None] | None = None,
    ocr_mode: str = "off",
    ocr_langs: str = "eng",
    pua_threshold: float = 0.20,
) -> "OcrOutcome":
    """Render a (potentially large) EPUB in chunks, with retry per chunk.

    For small books (spine items <= *chunk_size*) this falls through to a
    single render pass, still benefiting from retry and adaptive timeout.

    The text layer is rebuilt **per chunk** (before merging), not on the final
    merged PDF. This keeps every OCR job small (at most *chunk_size* pages),
    so a large book can't blow past the per-invocation OCR timeout. Returns an
    :class:`OcrOutcome` aggregating what happened across all units.
    """
    opf_path, opf_root, idrefs = extract_spine_idrefs(epub_path)

    # --- small book or chunking disabled → single render (no chunking overhead) ---
    if not idrefs or chunk_size <= 0 or len(idrefs) <= chunk_size:
        timeout = max(base_timeout, estimate_chunk_timeout(len(idrefs)))
        if progress_cb:
            progress_cb("Rendering PDF")
        _render_with_retry(
            epub_path, out_pdf, info,
            size=size, timeout=timeout,
            chromium_path=chromium_path, cwd=cwd,
            max_retries=max_retries,
        )
        if ocr_mode == "off":
            return OcrOutcome()
        if progress_cb:
            progress_cb("Checking / rebuilding text layer")
        return ocr_pdf_if_needed(
            out_pdf,
            mode=ocr_mode, langs=ocr_langs,
            page_direction=info.page_direction,
            pua_threshold=pua_threshold,
        )

    # --- large book → split, render + OCR each chunk, merge ---
    chunks = [idrefs[i:i + chunk_size]
              for i in range(0, len(idrefs), chunk_size)]
    work = cwd or out_pdf.parent
    chunk_pdfs: list[Path] = []
    outcome = OcrOutcome()

    try:
        for idx, chunk_idrefs in enumerate(chunks):
            n = len(chunks)
            if progress_cb:
                progress_cb(f"Rendering chunk {idx + 1}/{n}")

            chunk_epub = work / f"chunk_{idx}.epub"
            chunk_pdf = work / f"chunk_{idx}.pdf"

            _create_chunk_epub(
                epub_path, opf_path, opf_root,
                set(chunk_idrefs), chunk_epub,
            )

            timeout = max(base_timeout,
                          estimate_chunk_timeout(len(chunk_idrefs)))
            try:
                _render_with_retry(
                    chunk_epub, chunk_pdf, info,
                    size=size, timeout=timeout,
                    chromium_path=chromium_path, cwd=cwd,
                    max_retries=max_retries,
                )
            except EpubError as e:
                raise EpubError(
                    f"Chunk {idx + 1}/{n} failed after "
                    f"{1 + max(0, max_retries)} attempt(s): {e}"
                ) from e
            finally:
                chunk_epub.unlink(missing_ok=True)

            # Rebuild this chunk's text layer before it is merged. Doing it here
            # (rather than on the merged PDF) bounds each OCR job to chunk_size
            # pages.
            if ocr_mode != "off":
                if progress_cb:
                    progress_cb(f"Rebuilding text layer (chunk {idx + 1}/{n})")
                outcome.merge(ocr_pdf_if_needed(
                    chunk_pdf,
                    mode=ocr_mode, langs=ocr_langs,
                    page_direction=info.page_direction,
                    pua_threshold=pua_threshold,
                ))

            chunk_pdfs.append(chunk_pdf)

        if progress_cb:
            progress_cb("Merging chunks")
        merge_pdfs(chunk_pdfs, out_pdf)
    finally:
        for p in chunk_pdfs:
            p.unlink(missing_ok=True)

    return outcome


# --- Filename helpers -------------------------------------------------------
_SAFE_RE = re.compile(r"[^\w.\- ]", re.UNICODE)


def safe_filename(name: str) -> str:
    """Sanitise a filename: keep word chars (incl. CJK), dots, dashes, spaces."""
    name = name.replace("/", "_").replace("\\", "_").strip()
    name = _SAFE_RE.sub("", name)
    name = name.strip(" .") or "untitled"
    return name[:180]


# --- PUA Detection ----------------------------------------------------------

# Unicode Private Use Area ranges.
_PUA_RANGES = [
    (0xE000, 0xF8FF),      # BMP Private Use Area
    (0xF0000, 0xFFFFF),    # Supplementary Private Use Area-A
    (0x100000, 0x10FFFD),  # Supplementary Private Use Area-B
]


def _is_pua(cp: int) -> bool:
    """Return True if a codepoint falls in a Unicode Private Use Area."""
    return any(lo <= cp <= hi for lo, hi in _PUA_RANGES)


def detect_pua_text(pdf_path: Path, *, max_pages: int = 12) -> float:
    """Sample text from a PDF and return the fraction of PUA characters.

    Samples up to *max_pages* pages spread **evenly** across the document (so a
    book whose front matter is plain English doesn't mask PUA-obfuscated body
    text, and vice versa). Returns a float in [0.0, 1.0] representing the
    proportion of non-whitespace, non-ASCII characters that are PUA codepoints.
    A high value (e.g. >0.20) indicates the text layer is obfuscated with PUA
    font encoding.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return 0.0

    n = len(reader.pages)
    if n == 0:
        return 0.0

    if n <= max_pages:
        indices = range(n)
    else:
        step = n / max_pages
        indices = [int(i * step) for i in range(max_pages)]

    total_chars = 0
    pua_chars = 0
    for i in indices:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            continue
        for ch in text:
            cp = ord(ch)
            # Skip ASCII and whitespace — only count non-trivial characters.
            if cp <= 0x7F:
                continue
            total_chars += 1
            if _is_pua(cp):
                pua_chars += 1

    if total_chars == 0:
        return 0.0
    return pua_chars / total_chars


# --- OCR language validation ------------------------------------------------

@functools.lru_cache(maxsize=1)
def installed_tesseract_langs() -> frozenset[str]:
    """Return the set of language codes Tesseract has trained data for.

    Cached: shelling out is cheap but the installed set cannot change while the
    process runs. Returns an empty set if Tesseract is missing or the call
    fails — callers treat that as "cannot validate" and pass the request
    through unchanged (matching the previous behaviour).
    """
    try:
        proc = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()

    langs: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        # Skip the "List of available languages ..." header and blank lines.
        if not line or line.lower().startswith("list of"):
            continue
        langs.add(line)
    return frozenset(langs)


def resolve_ocr_langs(requested: str) -> tuple[str, list[str]]:
    """Filter a '+'-joined Tesseract language string down to installed models.

    Returns ``(usable, dropped)``. *usable* preserves the requested order and is
    safe to hand to ocrmypdf/Tesseract (a single missing model otherwise makes
    the whole OCR pass abort non-zero). *dropped* lists the requested codes
    whose data is not installed.

    If the installed set can't be determined, the request is returned unchanged.
    """
    requested_list = [code for code in requested.split("+") if code]
    available = installed_tesseract_langs()
    if not available:
        return requested, []
    usable = [code for code in requested_list if code in available]
    dropped = [code for code in requested_list if code not in available]
    return "+".join(usable), dropped


# --- OCR language mapping ---------------------------------------------------

def _map_lang_to_tesseract(tag: str) -> list[str] | None:
    """Map a BCP 47 language tag to Tesseract horizontal language codes.

    Returns a list of codes (ordered, most specific first), or None when the
    tag is unrecognised — callers fall back to the configured lang string.
    Vertical variants (chi_tra_vert, jpn_vert) are NOT included here; they are
    added downstream by add_text_layer based on page_direction.
    """
    t = tag.lower().strip()
    # Traditional Chinese: zh-TW, zh-HK, zh-MO, zh-Hant-*
    if any(s in t for s in ("zh-tw", "zh-hk", "zh-mo", "zh-hant")):
        return ["chi_tra"]
    # Simplified Chinese: zh-CN, zh-SG, zh-Hans-*
    if any(s in t for s in ("zh-cn", "zh-sg", "zh-hans")):
        return ["chi_sim"]
    # Chinese, script unspecified — include both, traditional first
    if t.startswith("zh"):
        return ["chi_tra", "chi_sim"]
    # Japanese
    if t.startswith("ja"):
        return ["jpn"]
    # Korean
    if t.startswith("ko"):
        return ["kor"]
    # English (and other Latin-script languages covered by eng)
    if t.startswith("en"):
        return ["eng"]
    return None


def build_ocr_langs(epub_language: str | None, configured_langs: str) -> str:
    """Build a focused Tesseract language string from the ePUB's language tag.

    For a recognised language, returns a targeted string — the primary script
    plus English (for embedded Latin text) — rather than the full configured
    list. Tesseract is measurably more accurate with fewer languages because its
    dictionary and N-gram models are not competing across unrelated scripts.

    Example outcomes:
      zh-TW  →  chi_tra+eng          (not chi_tra+chi_sim+jpn+kor+eng)
      ja     →  jpn+eng
      zh     →  chi_tra+chi_sim+eng

    Falls back to *configured_langs* when the tag is absent or unrecognised, so
    books in languages outside the mapping still OCR with the operator's chosen
    defaults.
    """
    if not epub_language:
        return configured_langs
    primary = _map_lang_to_tesseract(epub_language)
    if primary is None:
        return configured_langs
    langs = list(primary)
    if "eng" not in langs:
        langs.append("eng")
    return "+".join(langs)


# --- OCR Text Layer ---------------------------------------------------------

# Maximum time (seconds) to allow each ocrmypdf invocation. With per-chunk OCR
# this bounds a single chunk (<= CHUNK_SIZE pages), not the whole book.
_OCR_TIMEOUT_SEC = 1800


@dataclass
class OcrOutcome:
    """Aggregate result of rebuilding the text layer across one or more units.

    A "unit" is either the whole PDF (small books) or a single chunk PDF
    (large books). Counts let us distinguish a fully rebuilt book from a
    partially rebuilt one (some chunks failed) and report accordingly.
    """
    units_ocred: int = 0    # OCR ran and verification passed
    units_failed: int = 0   # OCR was attempted but failed / didn't clear PUA
    units_clean: int = 0    # auto mode: no obfuscation detected, skipped

    def merge(self, other: "OcrOutcome") -> None:
        self.units_ocred += other.units_ocred
        self.units_failed += other.units_failed
        self.units_clean += other.units_clean

    @property
    def applied(self) -> bool:
        return self.units_ocred > 0

    @property
    def any_failed(self) -> bool:
        return self.units_failed > 0

    def note(self) -> str | None:
        """Human-readable note for the library sidecar, or None if N/A."""
        if self.units_ocred and self.units_failed:
            return ("text layer partially rebuilt via OCR; "
                    f"{self.units_failed} section(s) may remain unselectable")
        if self.units_ocred:
            return "text layer rebuilt via OCR"
        if self.units_failed:
            return "OCR attempted but failed; text layer may be unusable"
        return None  # clean book, or OCR disabled


def ocr_pdf_if_needed(
    pdf_path: Path,
    *,
    mode: str,
    langs: str,
    page_direction: str | None,
    pua_threshold: float,
) -> OcrOutcome:
    """Decide whether a single PDF needs OCR and, if so, rebuild its text layer.

    *mode* is "auto" (OCR only when PUA obfuscation is detected), "always", or
    "off". Operates in place. Returns an :class:`OcrOutcome` for this one unit.
    """
    outcome = OcrOutcome()
    if mode == "off":
        return outcome

    if mode == "auto":
        if detect_pua_text(pdf_path) < pua_threshold:
            outcome.units_clean = 1
            return outcome
        reason = "pua"
    else:  # "always"
        reason = "always"

    ok = add_text_layer(
        pdf_path,
        langs=langs,
        page_direction=page_direction,
        reason=reason,
        pua_threshold=pua_threshold,
    )
    if ok:
        outcome.units_ocred = 1
    else:
        outcome.units_failed = 1
    return outcome


def add_text_layer(
    pdf_path: Path,
    *,
    langs: str,
    page_direction: str | None = None,
    reason: str = "always",
    pua_threshold: float = 0.20,
) -> bool:
    """Run OCR on the PDF to rebuild its text layer with real Unicode.

    When *reason* is ``"pua"``, the existing text layer is known to be
    PUA-obfuscated. ``--force-ocr`` is used (rasterizes pages, lays down a
    fresh Unicode layer) because ``--redo-ocr`` treats PUA text as valid and
    silently preserves it.

    When *reason* is ``"always"`` (generic mode), ``--redo-ocr`` is tried first
    to preserve vector glyphs, falling back to ``--force-ocr`` on failure.

    Language handling:
      * For vertical text (``page_direction == "rtl"``) the vertical Tesseract
        models are *preferred*, but only added if their data is installed.
      * Every requested language is validated against the installed set first;
        missing models are dropped instead of aborting the whole pass. If
        nothing usable remains, OCR is skipped and False is returned.

    After OCR the output is verified by re-running PUA detection; the rebuild
    counts as successful only if the PUA fraction drops below *pua_threshold*.

    Operates in-place on *pdf_path*. Returns True on verified success.
    """
    import shutil as _shutil

    from config import OCR_JOBS

    # Prefer vertical CJK models for vertical text (added before validation so
    # they're kept only when actually installed).
    if page_direction == "rtl":
        current = langs.split("+")
        for model in ("chi_tra_vert", "jpn_vert"):
            if model not in current:
                langs = langs + "+" + model

    # Drop any requested language whose data isn't installed — a single missing
    # model otherwise makes ocrmypdf abort with a non-zero exit, which is the
    # classic "OCR silently did nothing" failure.
    langs, dropped = resolve_ocr_langs(langs)
    if dropped:
        print(f"[ocr] dropping uninstalled Tesseract languages: {dropped}")
    if not langs:
        print("[ocr] no usable Tesseract languages installed; skipping OCR")
        return False

    out_path = pdf_path.with_suffix(".ocr.pdf")

    base_args = [
        "ocrmypdf",
        "-l", langs,
        "--jobs", str(OCR_JOBS),
        "--output-type", "pdf",
    ]

    def _run_ocr(strategy: str) -> bool:
        """Run ocrmypdf with the given strategy flag. Returns True on success."""
        cmd = base_args + [strategy, str(pdf_path), str(out_path)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_OCR_TIMEOUT_SEC,
            )
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                _shutil.move(str(out_path), str(pdf_path))
                return True
            # Surface the reason so a failed OCR isn't completely silent.
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
            if tail:
                print(f"[ocr] {strategy} failed: " + " | ".join(tail))
        except subprocess.TimeoutExpired:
            print(f"[ocr] {strategy} timed out after {_OCR_TIMEOUT_SEC}s")
        except FileNotFoundError:
            print("[ocr] ocrmypdf not found on PATH")
        finally:
            if out_path.exists():
                out_path.unlink(missing_ok=True)
        return False

    if reason == "pua":
        # PUA-detected: --force-ocr is the only reliable strategy.
        strategies = ["--force-ocr"]
    else:
        # Generic "always" mode: try --redo-ocr first (preserves vectors),
        # fall back to --force-ocr.
        strategies = ["--redo-ocr", "--force-ocr"]

    for strategy in strategies:
        if _run_ocr(strategy):
            # Verify the PUA text is actually gone.
            if detect_pua_text(pdf_path) < pua_threshold:
                return True
            # Still obfuscated — escalate to --force-ocr if not already tried.
            if strategy != "--force-ocr":
                continue
            return False

    return False
