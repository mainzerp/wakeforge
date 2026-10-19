"""API for the "Training" step: data overview, training job, runs with metrics and downloadable models."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from trainer import jobs, projects
from trainer.train.sources import advice, check, project_sources

router = APIRouter(prefix="/api/projects/{slug}/train")
RUN_RE = re.compile(r"^\d{8}-\d{6}-[a-z]+$")
FILE_RE = re.compile(r"^[a-z0-9_]+\.(tflite|json)$")
PROFILES = {
    "fast": {"label": "Fast", "steps": 10000, "hint": "a first model to try on the device"},
    "thorough": {"label": "Thorough", "steps": 40000, "hint": "the one to keep"},
}


class TrainBody(BaseModel):
    profile: Literal["fast", "thorough"] = "fast"
    target_faph: float = Field(default=0.25, gt=0, le=5)


def py_train() -> str:
    exe = shutil.which("py-train")
    if not exe:
        raise ValueError("py-train launcher not found: training only runs inside the container")
    return exe


def read_run(path: Path, full: bool = False) -> dict:
    try:
        data = json.loads((path / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"id": path.name, "state": "unknown"}
    if data.get("state") == "running" and data.get("job"):
        # A killed worker (container restart, SIGKILL) never writes its final state
        job_dir = path.parent.parent / "jobs" / data["job"]
        if not jobs.job_alive(job_dir, jobs.read_json(job_dir / "job.json")):
            data["state"] = "stopped"
    if not full:
        data.pop("history", None)
        data.pop("sources", None)
        if isinstance(data.get("calibration"), dict):
            data["calibration"] = {k: v for k, v in data["calibration"].items() if k != "curve"}
    return data


def run_dirs(path: Path) -> list[Path]:
    root = path / "runs"
    return sorted((d for d in root.iterdir() if RUN_RE.match(d.name)), reverse=True) if root.is_dir() else []


def run_dir(slug: str, run_id: str) -> Path:
    if not RUN_RE.match(run_id or ""):
        raise ValueError(f"invalid run: {run_id!r}")
    path = projects.project_dir(slug) / "runs" / run_id
    if not path.is_dir():
        raise FileNotFoundError(f"run not found: {run_id}")
    return path


@router.get("")
def status(slug: str) -> dict:
    path = projects.project_dir(slug)
    sources = project_sources(path)
    job = jobs.latest(path, "train")
    return {
        "sources": [s.summary() for s in sources],
        "problems": check(sources),
        "advice": advice(sources),
        "profiles": PROFILES,
        "runs": [read_run(d) for d in run_dirs(path)],
        "job": job,
        "active": jobs.active(),
    }


@router.post("/start")
def start(slug: str, body: TrainBody) -> dict:
    path = projects.project_dir(slug)
    problems = check(project_sources(path))
    if problems:
        raise ValueError("; ".join(problems))
    cmd = [py_train(), "-m", "trainer.train.run", "--project", str(path), "--profile", body.profile,
           "--target-faph", str(body.target_faph)]
    return jobs.start(path, "train", cmd, body.model_dump())


@router.get("/runs/{run_id}")
def run(slug: str, run_id: str) -> dict:
    return read_run(run_dir(slug, run_id), full=True)


@router.post("/runs/{run_id}/delete")
def delete_run(slug: str, run_id: str) -> dict:
    path = run_dir(slug, run_id)
    running = jobs.active()
    if running and running["project"] == slug and read_run(path).get("state") == "running":
        raise ValueError("stop the running training first")
    shutil.rmtree(path)
    return {"ok": True}


@router.get("/runs/{run_id}/files/{name}")
def run_file(slug: str, run_id: str, name: str) -> FileResponse:
    if not FILE_RE.match(name) or name == "run.json":
        raise ValueError("invalid file")
    path = run_dir(slug, run_id) / name
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {name}")
    media = "application/json" if name.endswith(".json") else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=name)
