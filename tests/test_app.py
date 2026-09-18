"""End-to-end test of the web flow for a PDF upload (auth bypassed)."""
from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    import config
    importlib.reload(config)
    import library, jobs
    importlib.reload(library)
    importlib.reload(jobs)
    import app as app_module
    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "current_user", lambda request: {"email": "t@example.com", "name": "T"})
    from fastapi.testclient import TestClient
    with TestClient(app_module.app) as c:
        yield c, app_module


def _wait(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/job-status/{job_id}").json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_pdf_upload_convert_download_delete(client):
    c, _app = client
    with (FIXTURES / "ja_vertical.pdf").open("rb") as fh:
        r = c.post("/upload", files={"file": ("ja_vertical.pdf", fh, "application/pdf")})
    assert r.status_code == 200, r.text
    filename = r.json()["filename"]
    assert filename.endswith(".pdf")

    r = c.post(f"/start-convert/{filename}")
    assert r.status_code == 200, r.text
    data = _wait(c, r.json()["job_id"])
    assert data["status"] == "done", data
    assert data["direction"] == "pdf-to-epub"
    assert [s["step"] for s in data["steps"]] == [1, 2, 3, 4, 5, "done"]
    assert "vertical" in data["steps"][2]["message"]
    out = data["output_name"]
    assert out.endswith("-pdf-to-epub.epub")

    r = c.get(f"/download/{out}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/epub+zip")
    assert r.content[:2] == b"PK"

    page = c.get("/library").text
    assert "badge-epub" in page and out in page
    home = c.get("/").text
    assert "file-badge epub" in home

    r = c.post(f"/delete/{out}")
    assert r.status_code == 200
    assert c.get(f"/download/{out}").status_code == 404


def test_rejects_other_extensions(client):
    c, _app = client
    r = c.post("/upload", files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400
