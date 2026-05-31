"""ePUB inspection and PDF conversion.

This module is deliberately dependency-light: ePUB structure is parsed with the
standard-library zipfile + xml.etree, and only Pillow is used (for cover
thumbnailing). The actual PDF rendering is delegated to the Vivliostyle CLI,
invoked as a subprocess.
"""
from __future__ import annotations

import io
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:  # pragma: no cover - Pillow should be installed
    _HAS_PIL = False


# Algorithms used purely for font obfuscation (NOT content DRM). An ePUB whose
# encryption.xml references only these is still readable.
_FONT_OBFUSCATION_ALGOS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

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


def effective_timeout(base_timeout: int, epub_path: Path, per_mb: int) -> int:
    """Return *base_timeout* extended by *per_mb* seconds for each MB of the ePUB."""
    try:
        size_mb = epub_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    return base_timeout + int(size_mb * per_mb)


def render_pdf_with_retries(
    epub_path: Path,
    out_pdf: Path,
    info: EpubInfo,
    *,
    size: str,
    timeout: int,
    chromium_path: str | None,
    cwd: Path | None = None,
    max_retries: int = 0,
    backoff: float = 1.5,
    on_retry: Callable[[int, int, int], None] | None = None,
) -> None:
    """Wrap :func:`render_pdf` with automatic retries and exponential timeout growth.

    *on_retry* is an optional callback ``(attempt, max_retries, timeout)``
    invoked before each retry so that callers can update progress indicators.
    """
    last_err: EpubError | None = None
    current_timeout = timeout

    for attempt in range(1 + max_retries):
        try:
            render_pdf(
                epub_path, out_pdf, info,
                size=size,
                timeout=current_timeout,
                chromium_path=chromium_path,
                cwd=cwd,
            )
            return  # success
        except EpubError as e:
            last_err = e
            # Don't retry hard errors (missing CLI, DRM, …)
            if "not found" in str(e).lower() or "DRM" in str(e):
                raise
            if attempt < max_retries:
                current_timeout = int(current_timeout * backoff)
                if on_retry is not None:
                    on_retry(attempt + 1, max_retries, current_timeout)
                # Clean up the failed output so the next attempt starts fresh
                if out_pdf.exists():
                    out_pdf.unlink(missing_ok=True)
            else:
                raise last_err


# --- Filename helpers -------------------------------------------------------
_SAFE_RE = re.compile(r"[^\w.\- ]", re.UNICODE)


def safe_filename(name: str) -> str:
    """Sanitise a filename: keep word chars (incl. CJK), dots, dashes, spaces."""
    name = name.replace("/", "_").replace("\\", "_").strip()
    name = _SAFE_RE.sub("", name)
    name = name.strip(" .") or "untitled"
    return name[:180]
