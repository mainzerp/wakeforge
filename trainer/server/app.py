"""FastAPI app: project and Sample Studio API plus the built Svelte UI.

  py-train -m uvicorn trainer.server.app:app --host 0.0.0.0 --port 8790
"""

from __future__ import annotations

import json
from typing import Literal
from urllib.parse import unquote

import soundfile as sf
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trainer import guide, jobs, projects
from trainer.paths import UI_DIST
from trainer.server import generate_api, mining_api, models_api, setup_api, train_api
from trainer.studio import store

MAX_UPLOAD_BYTES = 500 * 1024 * 1024

app = FastAPI(title="Wakeforge")
project_api = APIRouter(prefix="/api/projects")
studio_api = APIRouter(prefix="/api/projects/{slug}/studio")


@app.exception_handler(ValueError)
@app.exception_handler(sf.LibsndfileError)
async def bad_request(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    messages = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}".lstrip(": ") for e in exc.errors()]
    return JSONResponse({"error": "; ".join(messages)}, status_code=422)


@app.exception_handler(FileNotFoundError)
async def not_found(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=404)


@app.middleware("http")
async def revalidate_shell(request: Request, call_next):
    """Hashed assets may be cached forever, index.html must be revalidated so new builds show up."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/assets/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


class BorrowEntry(BaseModel):
    slug: str
    recordings: bool = False
    synthetic: bool = False


class ProjectBody(BaseModel):
    wake_word: str | None = None
    language: str | None = None
    tts_phrases: list[str] | None = None
    author: str | None = None
    website: str | None = None
    borrow_negatives: list[BorrowEntry] | None = None
    confusables: list[str] | None = None


@project_api.get("")
def list_projects() -> dict:
    out = projects.list_projects()
    for p in out:
        try:
            p["next"] = guide.guide(projects.project_dir(p["slug"]))["next"]
        except (OSError, ValueError, KeyError):
            p["next"] = None
    return {"projects": out}


@project_api.post("")
def create_project(body: ProjectBody) -> dict:
    with store.LOCK:
        return projects.create(body.model_dump())


@project_api.get("/{slug}")
def get_project(slug: str) -> dict:
    return projects.load(projects.project_dir(slug))


@project_api.patch("/{slug}")
def update_project(slug: str, body: ProjectBody) -> dict:
    with store.LOCK:
        return projects.update(slug, body.model_dump())


@project_api.get("/{slug}/guide")
def project_guide(slug: str) -> dict:
    return guide.guide(projects.project_dir(slug))


@project_api.get("/{slug}/borrow")
def borrow_candidates(slug: str) -> dict:
    return {"projects": projects.borrow_candidates(slug)}


class SessionRef(BaseModel):
    session: str


class SplitBody(SessionRef):
    params: dict | None = None


class RenameBody(SessionRef):
    prefix: str = ""


class LabelBody(SessionRef):
    clips: list[str]
    label: Literal["positive", "negative"] | None


class LabelUnsortedBody(SessionRef):
    label: Literal["positive", "negative"]


class ClipBody(SessionRef):
    clip: str


def studio_for(slug: str) -> store.Studio:
    return store.Studio(projects.project_dir(slug))


def with_counts(studio: store.Studio, session: dict) -> dict:
    return {"session": session, "counts": studio.sample_counts()}


@studio_api.get("/sessions")
def sessions(slug: str) -> dict:
    studio = studio_for(slug)
    return {"sessions": studio.list_sessions(), "counts": studio.sample_counts()}


@studio_api.post("/upload")
async def upload(slug: str, request: Request, x_split_params: str = Header("{}"), x_prefix: str = Header("")) -> dict:
    studio = studio_for(slug)
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload too large")
    params = store.parse_params(json.loads(x_split_params or "{}"))
    with store.LOCK:
        result = with_counts(studio, studio.new_session(body, params, unquote(x_prefix)))
        projects.touch(projects.project_dir(slug))
        return result


@studio_api.post("/split")
def split(slug: str, body: SplitBody) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        return with_counts(studio, studio.split(studio.session_dir(body.session), store.parse_params(body.params)))


@studio_api.post("/rename")
def rename(slug: str, body: RenameBody) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        return with_counts(studio, studio.rename(studio.session_dir(body.session), body.prefix))


@studio_api.post("/label")
def label(slug: str, body: LabelBody) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        return with_counts(studio, studio.label(studio.session_dir(body.session), body.clips, body.label))


@studio_api.post("/label_unsorted")
def label_unsorted(slug: str, body: LabelUnsortedBody) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        return with_counts(studio, studio.label_unsorted(studio.session_dir(body.session), body.label))


@studio_api.post("/delete_clip")
def delete_clip(slug: str, body: ClipBody) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        return with_counts(studio, studio.delete_clip(studio.session_dir(body.session), body.clip))


@studio_api.post("/delete_session")
def delete_session(slug: str, body: SessionRef) -> dict:
    studio = studio_for(slug)
    with store.LOCK:
        studio.delete_session(studio.session_dir(body.session))
    return {"ok": True, "counts": studio.sample_counts()}


@studio_api.get("/files/{session}/recording.wav")
def recording(slug: str, session: str) -> FileResponse:
    return FileResponse(studio_for(slug).session_dir(session) / "recording.wav", media_type="audio/wav")


@studio_api.get("/files/{session}/clips/{name}")
def clip(slug: str, session: str, name: str) -> FileResponse:
    return FileResponse(studio_for(slug).clip_path(session, name), media_type="audio/wav")


@app.get("/api/jobs/active")
def active_job() -> dict:
    return {"job": jobs.active()}


@project_api.post("/{slug}/jobs/{job_id}/stop")
def stop_job(slug: str, job_id: str) -> dict:
    return jobs.stop(jobs.job_dir(projects.project_dir(slug), job_id))


app.include_router(project_api)
app.include_router(studio_api)
app.include_router(generate_api.router)
app.include_router(generate_api.tts_router)
app.include_router(train_api.router)
app.include_router(mining_api.router)
app.include_router(models_api.router)
app.include_router(setup_api.router)

if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
