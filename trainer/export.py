"""Exported models of a project: a trained run's model with a chosen operating point, ready for ESPHome.

  <project>/models/<YYYYMMDD-HHMMSS>/<slug>.tflite   the run's streaming int8 model, unchanged
  <project>/models/<YYYYMMDD-HHMMSS>/<slug>.json     ESPHome manifest (v2) with the chosen probability_cutoff
  <project>/models/<YYYYMMDD-HHMMSS>/export.json     run, operating point and its measured metrics

The newest export is the project's current model. The server also serves it at a stable URL
(/api/projects/<slug>/models/current/<slug>.json), so ESPHome can fetch updates on every compile.
The manifest keeps only the keys ESPHome accepts: any extra key fails its config validation.

Pure Python (no TF): the server and the guide use it.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from trainer import projects
from trainer.mining import store as mining

EXPORT_RE = re.compile(r"^\d{8}-\d{6}$")
FILE_RE = re.compile(r"^[a-z0-9_]+\.(tflite|json)$")


def root(project: Path) -> Path:
    return project / "models"


def export_dirs(project: Path) -> list[Path]:
    r = root(project)
    return sorted((d for d in r.iterdir() if EXPORT_RE.match(d.name) and (d / "export.json").is_file()),
                  reverse=True) if r.is_dir() else []


def export_dir(project: Path, export_id: str) -> Path:
    if not EXPORT_RE.match(export_id or ""):
        raise ValueError(f"invalid export: {export_id!r}")
    path = root(project) / export_id
    if not (path / "export.json").is_file():
        raise FileNotFoundError(f"export not found: {export_id}")
    return path


def read(path: Path) -> dict:
    return json.loads((path / "export.json").read_text(encoding="utf-8"))


def current(project: Path) -> dict | None:
    dirs = export_dirs(project)
    return read(dirs[0]) if dirs else None


def speech_scores(project: Path, run_id: str) -> dict | None:
    """Trigger scores of the newest finished mining scan of a run, to count false accepts on real speech per cutoff.

    Only cutoffs at or above the scan's cutoff can be counted (lower scores were never collected). Triggers a person
    labeled as the real wake word are no false accepts and left out.
    """
    for d in mining.mine_dirs(project):
        meta = mining.read_json(d / "mine.json", {})
        if meta.get("run") != run_id or meta.get("state") != "done" or not meta.get("hours_done"):
            continue
        ls = mining.labels(d)
        scores = sorted((h["score"] for h in mining.hits(d) if ls.get(str(h["n"])) != "positive"), reverse=True)
        return {"id": d.name, "hours": meta["hours_done"], "min_cutoff": meta.get("mine_cutoff", meta.get("cutoff")),
                "window": meta.get("window"), "sources": meta.get("sources", []), "scores": scores}
    return None


def speech_faph(speech: dict | None, cutoff: float) -> float | None:
    if not speech or speech["min_cutoff"] is None or cutoff < speech["min_cutoff"]:
        return None
    return round(sum(1 for s in speech["scores"] if s > cutoff) / speech["hours"], 3)


def create(project: Path, run_dir: Path, cutoff: float) -> dict:
    """Copy a finished run's model into a new export with the given cutoff (one of the run's calibration points)."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cal, files = run.get("calibration") or {}, run.get("files") or {}
    if run.get("state") != "done" or not cal.get("curve") or not files:
        raise ValueError(f"run {run_dir.name} has no calibrated model")
    point = next((p for p in cal["curve"] if abs(p["cutoff"] - cutoff) < 1e-6), None)
    if point is None:
        raise ValueError(f"cutoff {cutoff} is not on the calibration curve of run {run_dir.name}")
    manifest = json.loads((run_dir / files["manifest"]).read_text(encoding="utf-8"))
    manifest["micro"]["probability_cutoff"] = point["cutoff"]
    # Name and credits come from the project as it is now (they are edited on the export page, after training)
    settings = projects.read(project)
    manifest.update(wake_word=settings.get("wake_word") or manifest.get("wake_word", ""),
                    author=settings.get("author") or "", website=settings.get("website") or "")

    export_id = f"{datetime.now():%Y%m%d-%H%M%S}"
    out = root(project) / export_id
    out.mkdir(parents=True)
    shutil.copy2(run_dir / files["model"], out / files["model"])
    (out / files["manifest"]).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    speech = speech_scores(project, run["id"])
    meta = {
        "id": export_id,
        "run": run["id"],
        "profile": run.get("profile"),
        "created": datetime.now().isoformat(timespec="seconds"),
        "wake_word": manifest.get("wake_word"),
        "author": manifest.get("author"),
        "website": manifest.get("website"),
        "probability_cutoff": point["cutoff"],
        "sliding_window_size": cal["sliding_window_size"],
        "calibrated_cutoff": cal["probability_cutoff"],
        "recall": point["recall"],
        "false_accepts_per_hour": point["faph"],
        "ambient_hours": cal.get("ambient_hours"),
        "speech_false_accepts_per_hour": speech_faph(speech, point["cutoff"]),
        "speech_hours": speech["hours"] if speech else None,
        "files": {"model": files["model"], "manifest": files["manifest"], "size": (out / files["model"]).stat().st_size},
    }
    (out / "export.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta
