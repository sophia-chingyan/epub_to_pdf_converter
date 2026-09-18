"""Filesystem-backed library of converted books.

Each book in the library consists of:
  <stem>.pdf | <stem>.epub   the converted file
  <stem>.meta.json           sidecar metadata (title, cover filename, ...)
  <stem>.cover.<ext>         optional cover thumbnail
"""
from __future__ import annotations

import json
from pathlib import Path

import config

BOOK_EXTENSIONS = (".pdf", ".epub")
MEDIA_TYPES = {".pdf": "application/pdf", ".epub": "application/epub+zip"}


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


def _book_files() -> list[Path]:
    files: list[Path] = []
    for ext in BOOK_EXTENSIONS:
        files.extend(config.LIBRARY_DIR.glob(f"*{ext}"))
    return files


def list_books(limit: int | None = None) -> list[dict]:
    """Return books newest-first in the shape the templates expect."""
    config.ensure_dirs()
    files = sorted(_book_files(), key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        files = files[:limit]

    books: list[dict] = []
    for f in files:
        base = f.name[: -len(f.suffix)]
        title, cover, note = f.stem, None, None
        meta_path = config.LIBRARY_DIR / f"{base}.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = meta.get("title") or title
                cover = meta.get("cover")
                note = meta.get("ocr_note")
            except Exception:
                pass
        if cover and not (config.LIBRARY_DIR / cover).exists():
            cover = None
        books.append({
            "stem": title,
            "cover": cover,
            "note": note,
            "files": [{
                "name": f.name,
                "ext": f.suffix.lstrip(".").upper(),
                "size": human_size(f.stat().st_size),
            }],
        })
    return books


_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def cover_path(name: str) -> Path | None:
    p = _safe_member(name)
    if p and p.exists() and p.is_file() and p.suffix.lower() in _COVER_EXTENSIONS:
        return p
    return None


def book_path(name: str) -> Path | None:
    """Path of a converted book (PDF or ePUB) by file name, or None."""
    p = _safe_member(name)
    if p and p.suffix.lower() in BOOK_EXTENSIONS and p.exists():
        return p
    return None


def pdf_path(name: str) -> Path | None:
    p = book_path(name)
    return p if p and p.suffix.lower() == ".pdf" else None


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def delete_book(name: str) -> bool:
    """Delete a book and its sidecar/cover. Returns True if the file existed."""
    p = book_path(name)
    if not p:
        return False
    base = p.name[: -len(p.suffix)]
    p.unlink(missing_ok=True)
    (config.LIBRARY_DIR / f"{base}.meta.json").unlink(missing_ok=True)
    for cover in config.LIBRARY_DIR.glob(f"{base}.cover.*"):
        cover.unlink(missing_ok=True)
    return True


def delete_all() -> list[str]:
    deleted = []
    for f in list(_book_files()):
        if delete_book(f.name):
            deleted.append(f.name)
    return deleted
