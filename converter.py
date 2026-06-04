"""ePUB inspection and PDF conversion.

This module is deliberately dependency-light: ePUB structure is parsed with the
standard-library zipfile + xml.etree, and only Pillow is used (for cover
thumbnailing). The actual PDF rendering is delegated to the Vivliostyle CLI,
invoked as a subprocess.
"""
from __future__ import annotations

import copy
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
        cover_href = _find_cover_href(opf_root)

        info = EpubInfo(
            title=title,
            fixed_layout=fixed_layout,
            page_direction=page_direction,
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
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Render a (potentially large) EPUB in chunks, with retry per chunk.

    For small books (spine items <= *chunk_size*) this falls through to a
    single render pass, still benefiting from retry and adaptive timeout.
    """
    opf_path, opf_root, idrefs = extract_spine_idrefs(epub_path)

    # --- small book or chunking disabled → single render (no chunking overhead) ---
    if not idrefs or chunk_size <= 0 or len(idrefs) <= chunk_size:
        timeout = max(base_timeout, estimate_chunk_timeout(len(idrefs)))
        _render_with_retry(
            epub_path, out_pdf, info,
            size=size, timeout=timeout,
            chromium_path=chromium_path, cwd=cwd,
            max_retries=max_retries,
        )
        return

    # --- large book → split, render chunks, merge ---
    chunks = [idrefs[i:i + chunk_size]
              for i in range(0, len(idrefs), chunk_size)]
    work = cwd or out_pdf.parent
    chunk_pdfs: list[Path] = []

    try:
        for idx, chunk_idrefs in enumerate(chunks):
            if progress_cb:
                progress_cb(idx + 1, len(chunks))

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
                chunk_pdfs.append(chunk_pdf)
            except EpubError as e:
                raise EpubError(
                    f"Chunk {idx + 1}/{len(chunks)} failed after "
                    f"{1 + max(0, max_retries)} attempt(s): {e}"
                ) from e
            finally:
                chunk_epub.unlink(missing_ok=True)

        merge_pdfs(chunk_pdfs, out_pdf)
    finally:
        for p in chunk_pdfs:
            p.unlink(missing_ok=True)


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


def detect_pua_text(pdf_path: Path, *, max_pages: int = 10) -> float:
    """Sample text from a PDF and return the fraction of PUA characters.

    Scans up to *max_pages* pages. Returns a float in [0.0, 1.0] representing
    the proportion of non-whitespace, non-ASCII characters that are PUA
    codepoints. A high value (e.g. >0.20) indicates the text layer is
    obfuscated with PUA font encoding.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return 0.0

    total_chars = 0
    pua_chars = 0

    pages_to_sample = min(max_pages, len(reader.pages))
    for i in range(pages_to_sample):
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


# --- OCR Text Layer ---------------------------------------------------------

# Maximum time (seconds) to allow each ocrmypdf invocation.
_OCR_TIMEOUT_SEC = 1800


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
    PUA-obfuscated.  In that case ``--force-ocr`` is used as the primary
    strategy (rasterizes pages, lays down a fresh Unicode layer) because
    ``--redo-ocr`` treats PUA text as valid and silently preserves it.

    When *reason* is ``"always"`` (generic mode), ``--redo-ocr`` is tried
    first to preserve vector glyphs, falling back to ``--force-ocr`` only on
    failure.

    After OCR, the output is verified by re-running PUA detection.  The
    rebuild is considered successful only if the PUA fraction drops below
    *pua_threshold*.

    When *page_direction* is "rtl" (common for vertical CJK), vertical
    Tesseract models are appended to *langs* if not already present.

    Operates in-place on *pdf_path*.  Returns True if OCR succeeded and
    verification passed, False otherwise.
    """
    import shutil as _shutil

    # Append vertical CJK models when page direction suggests vertical text.
    if page_direction == "rtl":
        existing = langs.split("+")
        vert_models = []
        for model in ("chi_tra_vert", "jpn_vert"):
            if model not in existing:
                vert_models.append(model)
        if vert_models:
            langs = langs + "+" + "+".join(vert_models)

    out_path = pdf_path.with_suffix(".ocr.pdf")

    # Build base ocrmypdf command arguments.
    base_args = [
        "ocrmypdf",
        "-l", langs,
        "--jobs", "2",
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
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        finally:
            if out_path.exists():
                out_path.unlink(missing_ok=True)
        return False

    if reason == "pua":
        # PUA-detected: --force-ocr is the only reliable strategy.
        # --redo-ocr treats PUA text as valid and preserves it.
        strategies = ["--force-ocr"]
    else:
        # Generic "always" mode: try --redo-ocr first (preserves vectors),
        # fall back to --force-ocr.
        strategies = ["--redo-ocr", "--force-ocr"]

    for strategy in strategies:
        if _run_ocr(strategy):
            # Verify the PUA text is actually gone.
            pua_fraction = detect_pua_text(pdf_path)
            if pua_fraction < pua_threshold:
                return True
            # PUA still present — if we haven't tried --force-ocr yet, escalate.
            if strategy != "--force-ocr":
                continue
            # --force-ocr also failed verification — give up.
            return False

    return False
