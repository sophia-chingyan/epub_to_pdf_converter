"""Single-slot, in-memory conversion job management.

Only one conversion runs at a time. Job state lives in memory (fine for a
single-user app); a process restart loses in-flight jobs but never the library,
which is on disk.
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

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_step": self.current_step,
            "current_label": self.current_label,
            "steps": self.steps,
            "error": self.error,
            "output_name": self.output_name,
        }


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
            job = Job(id=uuid.uuid4().hex, display_name=display_name)
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

    # --- the actual conversion pipeline ---
    def _run(self, job: Job, upload_path: Path) -> None:
        workdir = config.JOB_DIR / job.id
        try:
            workdir.mkdir(parents=True, exist_ok=True)

            self._begin(job, 1, "Validating ePUB")
            converter.validate(upload_path)
            self._complete(job, 1, "ePUB validated")

            self._begin(job, 2, "Extracting metadata & cover")
            info = converter.extract_info(upload_path)
            self._complete(job, 2, f"“{info.title}”")

            self._begin(job, 3, "Preparing Vivliostyle")
            layout = "fixed-layout" if info.fixed_layout else "reflowable"
            self._complete(job, 3, f"{layout} layout detected")

            self._begin(job, 4, "Rendering PDF (this is the slow step)")
            tmp_pdf = workdir / "output.pdf"
            converter.render_pdf(
                upload_path, tmp_pdf, info,
                size=config.REFLOWABLE_PAGE_SIZE,
                timeout=config.JOB_TIMEOUT_SEC,
                chromium_path=config.CHROMIUM_PATH,
                cwd=workdir,
            )
            self._complete(job, 4, "PDF rendered")

            self._begin(job, 5, "Saving to library")
            output_name = self._store(info, tmp_pdf)
            self._complete(job, 5, "Saved")

            self._finish(job, output_name)
        except EpubError as e:
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

    def _store(self, info: converter.EpubInfo, tmp_pdf: Path) -> str:
        """Move the PDF + cover + sidecar metadata into the library."""
        config.ensure_dirs()
        stem = converter.safe_filename(info.title)
        pdf_name = self._unique_name(stem, ".pdf", end_tag="-epub-to-pdf")
        base = pdf_name.removesuffix(".pdf")

        shutil.move(str(tmp_pdf), str(config.LIBRARY_DIR / pdf_name))

        cover_name = None
        if info.cover_bytes and info.cover_ext:
            cover_name = f"{base}.cover{info.cover_ext}"
            (config.LIBRARY_DIR / cover_name).write_bytes(info.cover_bytes)

        meta = {
            "title": info.title,
            "pdf": pdf_name,
            "cover": cover_name,
            "fixed_layout": info.fixed_layout,
        }
        (config.LIBRARY_DIR / f"{base}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        return pdf_name

    def _unique_name(self, stem: str, ext: str, *, end_tag: str = "") -> str:
        candidate = f"{stem}{end_tag}{ext}"
        i = 2
        while (config.LIBRARY_DIR / candidate).exists():
            candidate = f"{stem} ({i}){end_tag}{ext}"
            i += 1
        return candidate


manager = JobManager()
