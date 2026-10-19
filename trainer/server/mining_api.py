"""API for the "Mining" step: scan audio with a trained model, review the triggers, feed them back into training."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from trainer import jobs, projects, recipe
from trainer.mining import audio, store
from trainer.server.generate_api import py_omni
from trainer.server.train_api import py_train, read_run, run_dir, run_dirs
from trainer.tts import confusables

router = APIRouter(prefix="/api/projects/{slug}/mining")


class MineBody(BaseModel):
    run: str
    sources: list[str] = Field(min_length=1)
    cutoff: float | None = Field(default=None, ge=0.3, le=1.0)
    auto: bool = True  # the audio does not contain the wake word: the check may label clear false triggers


class LabelBody(BaseModel):
    hits: list[int] = Field(min_length=1)
    label: Literal["negative", "positive", "skip"] | None


class SoundAlikeBody(BaseModel):
    add: list[str] = []
    reject: list[str] = []


def models(path: Path) -> list[dict]:
    """Training runs that produced a calibrated model, newest first."""
    out = []
    for d in run_dirs(path):
        r = read_run(d)
        cal = r.get("calibration") or {}
        if "probability_cutoff" in cal and any(d.glob("*.tflite")):
            out.append({"run": r["id"], "profile": r.get("profile"), "started": r.get("started"),
                        "cutoff": cal["probability_cutoff"], "window": cal["sliding_window_size"],
                        "recall": cal.get("recall"), "faph": cal.get("false_accepts_per_hour")})
    return out


@router.get("")
def status(slug: str) -> dict:
    path = projects.project_dir(slug)
    data = projects.load(path)
    return {
        "sources": audio.sources(data["language"]),
        "models": models(path),
        "runs": [store.summary(d) for d in store.mine_dirs(path)],
        "totals": store.totals(path),
        "rounds": store.rounds(path),
        "recommended_rounds": recipe.MINING_ROUNDS,
        # A scan job checks its triggers itself; a separate check job runs for scans made before (newest wins).
        "job": max(filter(None, (jobs.latest(path, "mining"), jobs.latest(path, "triage"))), key=lambda j: j["id"], default=None),
        "active": jobs.active(),
    }


def sound_alike_candidates(path: Path, data: dict) -> list[dict]:
    return confusables.mined_candidates(path, data["tts_phrases"], data["language"],
                                        [*data["confusables"], *data["confusables_rejected"]])


@router.get("/sound-alikes")
def sound_alikes(slug: str) -> dict:
    """Phrases of the false triggers that sound close to the wake word and are not sound-alikes yet (espeak, CPU)."""
    path = projects.project_dir(slug)
    return {"candidates": sound_alike_candidates(path, projects.load(path))}


@router.post("/sound-alikes")
def take_sound_alikes(slug: str, body: SoundAlikeBody) -> dict:
    """Add phrases to the sound-alikes and/or turn others down so they are not offered again."""
    path = projects.project_dir(slug)
    data = projects.load(path)
    project = projects.update(slug, {"confusables": [*data["confusables"], *body.add],
                                     "confusables_rejected": [*data["confusables_rejected"], *body.reject]})
    return {"project": project, "candidates": sound_alike_candidates(path, project)}


@router.post("/start")
def start(slug: str, body: MineBody) -> dict:
    path = projects.project_dir(slug)
    run_dir(slug, body.run)
    if body.run not in {m["run"] for m in models(path)}:
        raise ValueError(f"run {body.run} has no calibrated model")
    known = {s["id"] for s in audio.sources(projects.load(path)["language"])}
    unknown = [s for s in body.sources if s not in known]
    if unknown:
        raise ValueError(f"unknown sources: {', '.join(unknown)}")
    cmd = [py_train(), "-m", "trainer.mining.run", "--project", str(path), "--run", body.run, "--sources", *body.sources]
    if body.cutoff is not None:
        cmd += ["--cutoff", str(body.cutoff)]
    if not body.auto:
        cmd.append("--no-auto")
    return jobs.start(path, "mining", cmd, body.model_dump())


@router.get("/runs/{mine_id}")
def mine_run(slug: str, mine_id: str) -> dict:
    path = store.mine_dir(projects.project_dir(slug), mine_id)
    labels, auto, triage = store.labels(path), store.auto(path), store.triage(path)
    hits = [{**h, "label": labels.get(str(h["n"])), "auto": str(h["n"]) in auto, "check": triage.get(str(h["n"]))}
            for h in store.hits(path)]
    return {"run": store.summary(path), "hits": hits}


@router.post("/runs/{mine_id}/check")
def check(slug: str, mine_id: str) -> dict:
    """Run the automatic check (again) for a scan, e.g. one made before the check existed."""
    path = projects.project_dir(slug)
    store.mine_dir(path, mine_id)
    cmd = [py_omni(), "-m", "trainer.mining.triage", "--project", str(path), "--mine", mine_id]
    return jobs.start(path, "triage", cmd, {"mine": mine_id})


@router.post("/runs/{mine_id}/label")
def label(slug: str, mine_id: str, body: LabelBody) -> dict:
    path = projects.project_dir(slug)
    return {"run": store.set_labels(path, mine_id, body.hits, body.label), "totals": store.totals(path)}


@router.post("/runs/{mine_id}/delete")
def delete(slug: str, mine_id: str) -> dict:
    path = projects.project_dir(slug)
    running = jobs.active()
    if running and running["project"] == slug and running.get("mine") == mine_id:
        raise ValueError("stop the running mining job first")
    store.delete(path, mine_id)
    return {"ok": True}


@router.get("/runs/{mine_id}/clips/{n}.wav")
def clip(slug: str, mine_id: str, n: int) -> FileResponse:
    path = store.mine_dir(projects.project_dir(slug), mine_id) / "clips" / f"{n:05d}.wav"
    if not path.is_file():
        raise FileNotFoundError(f"clip not found: {n}")
    return FileResponse(path, media_type="audio/wav")
