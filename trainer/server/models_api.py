"""API for the "Export" step: pick a run's operating point, export it and hand it to ESPHome (trainer/export.py)."""

from __future__ import annotations

import io
import shutil
import zipfile

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from trainer import export, projects
from trainer.server.train_api import read_run, run_dir, run_dirs

router = APIRouter(prefix="/api/projects/{slug}/models")


class ExportBody(BaseModel):
    run: str
    cutoff: float = Field(gt=0, lt=1)


@router.get("")
def status(slug: str) -> dict:
    path = projects.project_dir(slug)
    runs = []
    for d in run_dirs(path):
        r = read_run(d, full=True)
        if r.get("state") != "done" or not (r.get("calibration") or {}).get("curve") or not r.get("files"):
            continue
        r.pop("history", None)
        r.pop("sources", None)
        r["speech"] = export.speech_scores(path, r["id"])
        runs.append(r)
    exports = [export.read(d) for d in export.export_dirs(path)]
    return {"runs": runs, "exports": exports, "current": exports[0]["id"] if exports else None}


@router.post("")
def create(slug: str, body: ExportBody) -> dict:
    path = projects.project_dir(slug)
    return export.create(path, run_dir(slug, body.run), body.cutoff)


@router.post("/{export_id}/delete")
def delete(slug: str, export_id: str) -> dict:
    shutil.rmtree(export.export_dir(projects.project_dir(slug), export_id))
    return {"ok": True}


def file_response(slug: str, export_id: str | None, name: str) -> FileResponse:
    path = projects.project_dir(slug)
    if export_id is None:
        dirs = export.export_dirs(path)
        if not dirs:
            raise FileNotFoundError("no model exported yet")
        folder = dirs[0]
    else:
        folder = export.export_dir(path, export_id)
    if not export.FILE_RE.match(name) or not (folder / name).is_file():
        raise FileNotFoundError(f"file not found: {name}")
    media = "application/json" if name.endswith(".json") else "application/octet-stream"
    return FileResponse(folder / name, media_type=media, filename=name)


# ESPHome fetches the manifest, then the model next to it, and asks with HEAD whether they changed since the last
# compile. "current" always points to the newest export, so its URL can stay in the device's YAML.
@router.api_route("/current/{name}", methods=["GET", "HEAD"])
def current_file(slug: str, name: str) -> FileResponse:
    return file_response(slug, None, name)


@router.get("/{export_id}/files/{name}")
def export_file(slug: str, export_id: str, name: str) -> FileResponse:
    return file_response(slug, export_id, name)


@router.get("/{export_id}/zip")
def export_zip(slug: str, export_id: str) -> Response:
    folder = export.export_dir(projects.project_dir(slug), export_id)
    meta = export.read(folder)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in (meta["files"]["manifest"], meta["files"]["model"]):
            z.write(folder / name, name)
    stem = meta["files"]["manifest"].removesuffix(".json")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{stem}-{export_id}.zip"'})
