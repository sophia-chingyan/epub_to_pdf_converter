"""Filesystem-backed library of converted PDFs.

Each book in the library consists of:
  <stem>.pdf            the converted PDF
  <stem>.meta.json      sidecar metadata (title, cover filename, ...)
  <stem>.cover.<ext>    optional cover thumbnail
"""
from __future__ import annotations

import json
from pathlib import Path

import config


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _safe_member(name: str) -> Path | None:
    """Resolve a library filename to a path, refusing traversal."""
    base = Path(name).name  # strip any directory component
    if not base or base != name:
        return None
    p = (config.LIBRARY_DIR / base).resolve()
    try:
        p.relative_to(config.LIBRARY_DIR)
    except ValueError:
        return None
    return p


def list_books(limit: int | None = None) -> list[dict]:
    """Return books newest-first in the shape the templates expect."""
    config.ensure_dirs()
    pdfs = sorted(
        config.LIBRARY_DIR.glob("*.pdf"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if limit is not None:
        pdfs = pdfs[:limit]

    books: list[dict] = []
    for pdf in pdfs:
        base = pdf.name[:-4]
        title, cover = pdf.stem, None
        meta_path = config.LIBRARY_DIR / f"{base}.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = meta.get("title") or title
                cover = meta.get("cover")
            except Exception:
                pass
        if cover and not (config.LIBRARY_DIR / cover).exists():
            cover = None
        books.append({
            "stem": title,
            "cover": cover,
            "files": [{
                "name": pdf.name,
                "ext": "PDF",
                "size": human_size(pdf.stat().st_size),
            }],
        })
    return books


def cover_path(name: str) -> Path | None:
    p = _safe_member(name)
    if p and p.exists() and p.is_file():
        return p
    return None


def pdf_path(name: str) -> Path | None:
    p = _safe_member(name)
    if p and p.suffix.lower() == ".pdf" and p.exists():
        return p
    return None


def delete_book(pdf_name: str) -> bool:
    """Delete a PDF and its sidecar/cover. Returns True if the PDF existed."""
    p = pdf_path(pdf_name)
    if not p:
        return False
    base = p.name[:-4]
    p.unlink(missing_ok=True)
    (config.LIBRARY_DIR / f"{base}.meta.json").unlink(missing_ok=True)
    for cover in config.LIBRARY_DIR.glob(f"{base}.cover.*"):
        cover.unlink(missing_ok=True)
    return True


def delete_all() -> list[str]:
    deleted = []
    for pdf in list(config.LIBRARY_DIR.glob("*.pdf")):
        if delete_book(pdf.name):
            deleted.append(pdf.name)
    return deleted
