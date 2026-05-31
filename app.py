"""ePUB -> PDF converter web application.

Routes:
  GET  /                    Convert page (or login page when signed out)
  GET  /library             Library page
  GET  /login               Begin Google OAuth
  GET  /auth                OAuth callback
  GET  /logout              Clear session
  POST /upload              Receive an .epub, return its stored name
  POST /start-convert/{f}   Start a conversion job
  GET  /job-status/{id}     Poll job progress
  GET  /download/{name}     Download a converted PDF
  GET  /cover/{name}        Serve a cover thumbnail
  POST /delete/{name}       Delete one book
  POST /delete-all          Delete all books
"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
import converter
import library
from auth import current_user, is_allowed, oauth, redirect_uri
from jobs import manager

config.ensure_dirs()

app = FastAPI(title="ePUB to PDF Converter")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    https_only=config.https_only(),
    same_site="lax",
)

templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def _sweep_temp() -> None:
    """Clear orphaned temp files left by any crashed/interrupted jobs."""
    for d in (config.UPLOAD_DIR, config.JOB_DIR):
        if d.exists():
            for child in d.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)


def _render(request: Request, name: str, **ctx):
    # Starlette's modern signature takes the request first.
    return templates.TemplateResponse(request, name, ctx)


def _ctx(user: dict, **extra) -> dict:
    return {
        "user_name": user.get("name", ""),
        "user_picture": user.get("picture", ""),
        **extra,
    }


# --- Pages ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if not user:
        return _render(request, "login.html")
    books = library.list_books(limit=config.RECENT_COUNT)
    return _render(request, "index.html", books=books, **_ctx(user))


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    user = current_user(request)
    if not user:
        return _render(request, "login.html")
    books = library.list_books()
    return _render(request, "library.html", books=books, **_ctx(user))


# --- Auth -------------------------------------------------------------------
@app.get("/login")
async def login(request: Request):
    return await oauth.google.authorize_redirect(request, redirect_uri())


@app.get("/auth")
async def auth(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return _render(request, "login_error.html",
                       error="Sign-in failed. Please try again.")
    info = token.get("userinfo") or {}
    email = info.get("email")
    if not is_allowed(email):
        return _render(request, "login_error.html",
                       error=f"{email or 'This account'} is not authorised to use this app.")
    request.session["user"] = {
        "email": email,
        "name": info.get("name", email),
        "picture": info.get("picture", ""),
    }
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# --- Conversion API ---------------------------------------------------------
@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)

    orig = file.filename or "book.epub"
    if not orig.lower().endswith(".epub"):
        return JSONResponse({"error": "Please upload an .epub file."}, status_code=400)

    safe = converter.safe_filename(orig)
    if not safe.lower().endswith(".epub"):
        safe += ".epub"
    dest = config.UPLOAD_DIR / safe

    size = 0
    config.ensure_dirs()
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"File exceeds the {config.MAX_UPLOAD_MB} MB limit."},
                    status_code=400,
                )
            out.write(chunk)

    return JSONResponse({"filename": safe, "size_mb": round(size / (1024 * 1024), 1)})


@app.post("/start-convert/{filename}")
def start_convert(request: Request, filename: str):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)

    safe = Path(filename).name
    upload_path = config.UPLOAD_DIR / safe
    if safe != filename or not upload_path.exists():
        return JSONResponse({"error": "Upload not found. Please re-upload."}, status_code=404)

    try:
        job_id = manager.start(upload_path, display_name=safe)
    except RuntimeError:
        return JSONResponse(
            {"error": "A conversion is already running. Please wait for it to finish."},
            status_code=409,
        )
    return JSONResponse({"job_id": job_id})


@app.get("/job-status/{job_id}")
def job_status(request: Request, job_id: str):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    job = manager.get(job_id)
    if not job:
        return JSONResponse({"error": "Unknown job."}, status_code=404)
    return JSONResponse(job.to_dict())


# --- Files ------------------------------------------------------------------
@app.get("/download/{name}")
def download(request: Request, name: str):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    p = library.pdf_path(name)
    if not p:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return FileResponse(p, media_type="application/pdf", filename=p.name)


@app.get("/cover/{name}")
def cover(request: Request, name: str):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    p = library.cover_path(name)
    if not p:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return FileResponse(p)


@app.post("/delete/{name}")
def delete(request: Request, name: str):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    if library.delete_book(name):
        return JSONResponse({"deleted": name})
    return JSONResponse({"error": "Not found."}, status_code=404)


@app.post("/delete-all")
def delete_all(request: Request):
    if not current_user(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    return JSONResponse({"deleted": library.delete_all()})
