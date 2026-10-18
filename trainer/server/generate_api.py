"""API for the "Synthetic samples" step: OmniVoice corpus status, generation job, previews and listening."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator

from trainer import jobs, projects
from trainer.paths import DATA
from trainer.tts import confusables, corpus
from trainer.tts.voices import load_bank

router = APIRouter(prefix="/api/projects/{slug}/generate")
tts_router = APIRouter(prefix="/api/tts")
PREVIEW_TIMEOUT_S = 300
PREVIEWS = DATA / "cache" / "preview"
KEEP_PREVIEWS = 20
Kind = Literal["positive", "negative", "confusable"]
ENGINE = {"name": "OmniVoice", "model": "k2-fsa/OmniVoice", "license": "CC-BY-NC 4.0 weights"}


class Range(BaseModel):
    lo: float
    hi: float

    @model_validator(mode="after")
    def ordered(self) -> "Range":
        if self.lo > self.hi:
            raise ValueError("range minimum is larger than maximum")
        if self.lo < 0.5 or self.hi > 2.0:
            raise ValueError("speed must stay within 0.5..2.0")
        return self


class GenerateBody(BaseModel):
    count: int = Field(ge=100, le=500_000)
    speed: Range = Range(lo=0.85, hi=1.2)
    steps: int = Field(default=16, ge=8, le=64)
    design_share: float = Field(default=0.15, ge=0.0, le=1.0)
    filter: bool = True


class ConfusableBody(BaseModel):
    count: int = Field(ge=100, le=200_000)
    speed: Range = Range(lo=0.85, hi=1.2)
    steps: int = Field(default=16, ge=8, le=64)
    design_share: float = Field(default=0.15, ge=0.0, le=1.0)


class SoundAlikeBody(BaseModel):
    language: str = Field(pattern=r"^[a-z]{2}$")
    phrases: list[str] = Field(min_length=1, max_length=8)
    words: list[str] = Field(default=[], max_length=projects.MAX_CONFUSABLES)
    suggest: int = Field(default=0, ge=0, le=60)
    project: str | None = None  # also suggest words from this project's mined false triggers


class PreviewBody(BaseModel):
    language: str = Field(pattern=r"^[a-z]{2}$")
    phrases: list[str] = Field(min_length=1, max_length=8)
    n: int = Field(default=2, ge=1, le=6)
    speed: Range = Range(lo=0.85, hi=1.2)
    steps: int = Field(default=16, ge=8, le=64)

    @model_validator(mode="after")
    def clean(self) -> "PreviewBody":
        self.phrases = list(dict.fromkeys(p.strip() for p in self.phrases if p.strip()))
        if not self.phrases or any(len(p) > 60 for p in self.phrases):
            raise ValueError("phrases must be 1-60 characters")
        return self


def py_omni() -> str:
    exe = shutil.which("py-omni")
    if not exe:
        raise ValueError("py-omni launcher not found: generation only runs inside the container")
    return exe


def preview_key(body: PreviewBody) -> str:
    return hashlib.sha1(json.dumps([body.language, body.phrases, time.time()]).encode()).hexdigest()[:12]


@router.get("")
def status(slug: str) -> dict:
    path = projects.project_dir(slug)
    project = projects.load(path)
    index = corpus.load_index(corpus.corpus_dir(path))
    negatives = corpus.load_index(corpus.negative_dir(path))
    sound_alikes = corpus.load_index(corpus.confusable_dir(path))
    return {
        "engine": {**ENGINE, "voices": len(load_bank(project["language"]))},
        "corpus": {k: index[k] for k in ("voice", "phrases", "total", "seconds")} | {"shards": len(index["shards"])},
        "negatives": {"total": negatives["total"], "seconds": negatives["seconds"]},
        "confusables": {k: sound_alikes[k] for k in ("phrases", "total", "seconds")},
        "job": jobs.latest(path, "generate"),
        "confusable_job": jobs.latest(path, "confusables"),
        "active": jobs.active(),
    }


@router.post("/confusables/start")
def start_confusables(slug: str, body: ConfusableBody) -> dict:
    path = projects.project_dir(slug)
    if not projects.load(path)["confusables"]:
        raise ValueError("add sound-alike words in the wake word step first")
    cmd = [py_omni(), "-m", "trainer.tts.omni", "run", "--mode", "confusable", "--project", str(path),
           "--count", str(body.count), "--speed", str(body.speed.lo), str(body.speed.hi), "--steps", str(body.steps),
           "--design-share", str(body.design_share)]
    return jobs.start(path, "confusables", cmd, body.model_dump())


@router.post("/confusables/reset")
def reset_confusables(slug: str) -> dict:
    path = projects.project_dir(slug)
    running = jobs.active()
    if running and running["project"] == slug:
        raise ValueError("stop the running job first")
    corpus.reset(corpus.confusable_dir(path))
    return status(slug)


@router.post("/start")
def start(slug: str, body: GenerateBody) -> dict:
    path = projects.project_dir(slug)
    cmd = [py_omni(), "-m", "trainer.tts.omni", "run", "--project", str(path), "--count", str(body.count),
           "--speed", str(body.speed.lo), str(body.speed.hi), "--steps", str(body.steps),
           "--design-share", str(body.design_share)]
    if not body.filter:
        cmd.append("--no-filter")
    return jobs.start(path, "generate", cmd, body.model_dump())


@router.post("/reset")
def reset(slug: str) -> dict:
    path = projects.project_dir(slug)
    running = jobs.active()
    if running and running["project"] == slug:
        raise ValueError("stop the running job first")
    corpus.reset(corpus.corpus_dir(path))
    corpus.reset(corpus.negative_dir(path))
    return status(slug)


@tts_router.post("/preview")
def preview(body: PreviewBody) -> dict:
    """Pronunciation check without a project: n clips per spelling plus the phonetic transcript."""
    running = jobs.active()
    if running:
        raise ValueError(f"the GPU is busy with a {running['kind']} job; check the pronunciation after it finished")
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    for old in sorted(PREVIEWS.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_PREVIEWS]:
        shutil.rmtree(old, ignore_errors=True)
    key = preview_key(body)
    out = PREVIEWS / key
    cmd = [py_omni(), "-m", "trainer.tts.omni", "preview", "--language", body.language, "--phrases", *body.phrases,
           "--n", str(body.n), "--out", str(out), "--speed", str(body.speed.lo), str(body.speed.hi),
           "--steps", str(body.steps)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=PREVIEW_TIMEOUT_S)
    if res.returncode:
        lines = [line for line in res.stderr.strip().splitlines() if line.strip()]
        raise ValueError(f"preview failed: {lines[-1] if lines else res.returncode}")
    result = json.loads(res.stdout.strip().splitlines()[-1])
    for clip in result["clips"]:
        clip["url"] = f"/api/tts/preview/{key}/{clip.pop('file')}"
    return result


@tts_router.get("/preview/{key}/{name}")
def preview_file(key: str, name: str) -> FileResponse:
    if not (key.isalnum() and name.removesuffix(".wav").isdigit()):
        raise ValueError("invalid preview file")
    path = PREVIEWS / key / name
    if not path.is_file():
        raise FileNotFoundError("preview not found")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@tts_router.post("/sound-alikes")
def sound_alikes(body: SoundAlikeBody) -> dict:
    """Phone distance of the given sound-alikes to the spellings, plus `suggest` new ones (CPU, espeak only)."""
    words = list(dict.fromkeys(w.strip() for w in body.words if w.strip()))
    scored = confusables.score(words, body.phrases, body.language) if words else []
    rejected, mined = [], []
    if body.project and body.suggest:
        path = projects.project_dir(body.project)
        rejected = projects.load(path)["confusables_rejected"]
        mined = confusables.mined_candidates(path, body.phrases, body.language, [*words, *rejected])
    suggestions = confusables.suggest(body.phrases, body.language, body.suggest, [*words, *rejected], mined) if body.suggest else []
    return {
        "words": [{"text": w, "distance": d, "level": confusables.level(d)} for w, d in zip(words, scored)],
        "suggestions": [{**s, "level": confusables.level(s["distance"])} for s in suggestions],
    }


def kind_dir(slug: str, kind: Kind) -> Path:
    path = projects.project_dir(slug)
    return {"positive": corpus.corpus_dir, "negative": corpus.negative_dir, "confusable": corpus.confusable_dir}[kind](path)


@router.get("/samples")
def samples(slug: str, n: int = 12, kind: Kind = "positive") -> dict:
    clips = corpus.random_clips(kind_dir(slug, kind), max(1, min(n, 48)), np.random.default_rng())
    for c in clips:
        c["url"] = f"/api/projects/{slug}/generate/clip/{kind}/{c['shard']}/{c['clip']}.wav"
    return {"clips": clips}


@router.get("/clip/{kind}/{shard}/{clip}.wav")
def clip(slug: str, kind: Kind, shard: int, clip: int) -> Response:
    audio, _ = corpus.read_clip(kind_dir(slug, kind), shard, clip)
    return Response(corpus.wav_bytes(audio), media_type="audio/wav", headers={"Cache-Control": "max-age=3600"})
