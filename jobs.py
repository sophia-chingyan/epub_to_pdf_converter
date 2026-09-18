"""Single-slot, in-memory conversion job management.

Only one conversion runs at a time. Job state lives in memory (fine for a
single-user app); a process restart loses in-flight jobs but never the library,
which is on disk.

Two pipelines share this manager, chosen by the upload's extension:

  .epub → PDF   (converter.py, Vivliostyle)
  .pdf  → ePUB  (pdf2epub.py, PyMuPDF)

Both report the same five steps so the progress UI is identical.
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import config
import converter
from converter import EpubError

try:
    from pdf2epub import PdfError
except Exception:  # pragma: no cover - PyMuPDF not installed: ePUB→PDF still works
    class PdfError(Exception):  # type: ignore[no-redef]
        """Placeholder so the except clause below stays valid."""

# Output file suffix appended before the extension, per direction.
END_TAGS = {".pdf": "-epub-to-pdf", ".epub": "-pdf-to-epub"}


@dataclass
class Job:
    id: str
    display_name: str
    status: str = "running"            # running | done | error
    current_step: int = 0
    current_label: str = ""
    steps: list[dict] = field(default_factory=list)  # completed steps
    error: str = ""
    output_name: str | None = None
    direction: str = "epub-to-pdf"     # epub-to-pdf | pdf-to-epub

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_step": self.current_step,
            "current_label": self.current_label,
            "steps": self.steps,
            "error": self.error,
            "output_name": self.output_name,
            "direction": self.direction,
        }


def direction_for(path: Path) -> str:
    return "pdf-to-epub" if path.suffix.lower() == ".pdf" else "epub-to-pdf"


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active_id: str | None = None

    def _busy_unlocked(self) -> bool:
        if self._active_id is None:
            return False
        job = self._jobs.get(self._active_id)
        return bool(job and job.status == "running")

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy_unlocked()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, upload_path: Path, display_name: str) -> str:
        with self._lock:
            if self._busy_unlocked():
                raise RuntimeError("A conversion is already in progress.")
            job = Job(id=uuid.uuid4().hex, display_name=display_name,
                      direction=direction_for(upload_path))
            self._jobs[job.id] = job
            self._active_id = job.id
        threading.Thread(
            target=self._run, args=(job, upload_path), daemon=True
        ).start()
        return job.id

    # --- step helpers (thread-safe writes) ---
    def _begin(self, job: Job, n: int, label: str) -> None:
        with self._lock:
            job.current_step = n
            job.current_label = label

    def _complete(self, job: Job, n: int, message: str) -> None:
        with self._lock:
            job.steps.append({"step": n, "message": message})

    def _finish(self, job: Job, output_name: str) -> None:
        with self._lock:
            job.output_name = output_name
            job.steps.append({"step": "done", "message": "Saved to library"})
            job.status = "done"
            job.current_step = 0

    def _fail(self, job: Job, message: str) -> None:
        with self._lock:
            job.status = "error"
            job.error = message
            job.current_step = 0

    # --- dispatcher ---
    def _run(self, job: Job, upload_path: Path) -> None:
        workdir = config.JOB_DIR / job.id
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            if job.direction == "pdf-to-epub":
                self._run_pdf_to_epub(job, upload_path, workdir)
            else:
                self._run_epub_to_pdf(job, upload_path, workdir)
        except (EpubError, PdfError) as e:
            self._fail(job, str(e))
        except Exception as e:  # pragma: no cover - defensive
            self._fail(job, f"Unexpected error: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                upload_path.unlink(missing_ok=True)
            except Exception:
                pass
            with self._lock:
                if self._active_id == job.id:
                    self._active_id = None

    # --- ePUB → PDF ---
    def _run_epub_to_pdf(self, job: Job, upload_path: Path, workdir: Path) -> None:
        self._begin(job, 1, "Validating ePUB")
        converter.validate(upload_path)
        self._complete(job, 1, "ePUB validated")

        self._begin(job, 2, "Extracting metadata & cover")
        info = converter.extract_info(upload_path)
        self._complete(job, 2, f"“{info.title}”")

        # Derive a focused OCR language string from the ePUB's dc:language tag
        # (e.g. "zh-TW" → "chi_tra+eng") for better Tesseract accuracy.
        # Falls back to the operator-configured OCR_LANGS for unknown languages.
        ocr_langs = converter.build_ocr_langs(info.epub_language, config.OCR_LANGS)

        self._begin(job, 3, "Preparing Vivliostyle")
        layout = "fixed-layout" if info.fixed_layout else "reflowable"
        self._complete(job, 3, f"{layout} layout detected")

        # Step 4: render + rebuild text layer. The text layer is rebuilt
        # per chunk inside render_pdf_chunked (so each OCR job stays small),
        # which is why detection/OCR is no longer a separate step here.
        self._begin(job, 4, "Rendering PDF (this is the slow step)")
        tmp_pdf = workdir / "output.pdf"

        def on_progress(message: str) -> None:
            self._begin(job, 4, message)

        outcome = converter.render_pdf_chunked(
            upload_path, tmp_pdf, info,
            size=config.REFLOWABLE_PAGE_SIZE,
            base_timeout=config.JOB_TIMEOUT_SEC,
            chromium_path=config.CHROMIUM_PATH,
            cwd=workdir,
            chunk_size=config.CHUNK_SIZE,
            max_retries=config.CHUNK_MAX_RETRIES,
            progress_cb=on_progress,
            ocr_mode=config.TEXT_LAYER_MODE,
            ocr_langs=ocr_langs,
            pua_threshold=config.PUA_THRESHOLD,
        )

        if outcome.applied and outcome.any_failed:
            render_msg = "PDF rendered; text layer partially rebuilt via OCR"
        elif outcome.applied:
            render_msg = "PDF rendered; text layer rebuilt via OCR"
        elif outcome.any_failed:
            render_msg = "PDF rendered; OCR failed (visual PDF is correct)"
        else:
            render_msg = "PDF rendered"
        self._complete(job, 4, render_msg)

        self._begin(job, 5, "Saving to library")
        output_name = self._store(
            title=info.title, tmp_file=tmp_pdf, ext=".pdf",
            cover_bytes=info.cover_bytes, cover_ext=info.cover_ext,
            extra={"fixed_layout": info.fixed_layout}, note=outcome.note(),
        )
        self._complete(job, 5, "Saved")
        self._finish(job, output_name)

    # --- PDF → ePUB ---
    def _run_pdf_to_epub(self, job: Job, upload_path: Path, workdir: Path) -> None:
        import pdf2epub

        self._begin(job, 1, "Validating PDF")
        pdf2epub.validate(upload_path)
        self._complete(job, 1, "PDF validated")

        self._begin(job, 2, "Extracting metadata & cover")
        info = pdf2epub.extract_info(upload_path)
        self._complete(job, 2, f"“{info.title}” ({info.page_count} pages)")

        # Step 3: layout analysis (writing mode, language, headers/footers).
        # OCR for scanned or PUA-obfuscated PDFs happens here too, in place on
        # the uploaded copy (which is deleted after the job either way).
        self._begin(job, 3, "Analysing layout")

        def on_analysis(message: str) -> None:
            self._begin(job, 3, message)

        analysis = pdf2epub.analyze(upload_path, on_analysis)
        ocr_langs = converter.build_ocr_langs(info.language, config.OCR_LANGS)
        if not info.language:
            ocr_langs = converter.build_ocr_langs(analysis.language, config.OCR_LANGS)
        analysis, ocr_note = pdf2epub.prepare_text_layer(
            upload_path, analysis,
            mode=config.PDF_OCR_MODE, langs=ocr_langs, jobs=config.OCR_JOBS,
            pua_threshold=config.PUA_THRESHOLD, progress_cb=on_analysis,
        )
        mode = "vertical (縦書き)" if analysis.vertical else "horizontal"
        language = pdf2epub.resolve_language(info.language, analysis.language)
        self._complete(job, 3, f"{mode} text, language {language}")

        self._begin(job, 4, "Building ePUB")
        tmp_epub = workdir / "output.epub"

        def on_progress(message: str) -> None:
            self._begin(job, 4, message)

        result = pdf2epub.convert(
            upload_path, tmp_epub,
            title=info.title, author=info.author,
            progress_cb=on_progress,
            ocr_mode="off",           # already handled above
            tables=config.PDF_TABLES,
            drawings=config.PDF_DRAWINGS,
        )
        parts = [f"{result.chapter_count} section(s)", f"{result.image_count} image(s)"]
        self._complete(job, 4, "ePUB built: " + ", ".join(parts))

        self._begin(job, 5, "Saving to library")
        note = ocr_note or (result.notes[0] if result.notes else None)
        output_name = self._store(
            title=result.title, tmp_file=tmp_epub, ext=".epub",
            cover_bytes=info.cover_bytes, cover_ext=info.cover_ext,
            extra={"language": result.language, "vertical": result.vertical,
                   "pages": result.page_count},
            note=note,
        )
        self._complete(job, 5, "Saved")
        self._finish(job, output_name)

    # --- library storage ---
    def _store(self, *, title: str, tmp_file: Path, ext: str,
               cover_bytes: bytes | None, cover_ext: str | None,
               extra: dict | None = None, note: str | None = None) -> str:
        """Move the output + cover + sidecar metadata into the library."""
        config.ensure_dirs()
        stem = converter.safe_filename(title)
        out_name = self._unique_name(stem, ext, end_tag=END_TAGS.get(ext, ""))
        base = out_name.removesuffix(ext)

        shutil.move(str(tmp_file), str(config.LIBRARY_DIR / out_name))

        cover_name = None
        if cover_bytes and cover_ext:
            cover_name = f"{base}.cover{cover_ext}"
            (config.LIBRARY_DIR / cover_name).write_bytes(cover_bytes)

        meta = {
            "title": title,
            "file": out_name,
            "format": ext.lstrip(".").upper(),
            "cover": cover_name,
            **(extra or {}),
        }
        if ext == ".pdf":
            meta["pdf"] = out_name  # key kept for sidecars written by older versions
        if note:
            meta["ocr_note"] = note
        (config.LIBRARY_DIR / f"{base}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        return out_name

    def _unique_name(self, stem: str, ext: str, *, end_tag: str = "") -> str:
        candidate = f"{stem}{end_tag}{ext}"
        i = 2
        while (config.LIBRARY_DIR / candidate).exists():
            candidate = f"{stem} ({i}){end_tag}{ext}"
            i += 1
        return candidate


manager = JobManager()
