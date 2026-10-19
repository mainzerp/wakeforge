"""Session storage for the Sample Studio of one project.

A session is a directory below <project>/recordings holding recording.wav, clips/, clips.json and session.json.
A clip's label is derived from which sample set below <project>/samples holds a copy of it.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

import soundfile as sf

from trainer.studio.splitter import SplitParams, load_prefix, load_session, rename_prefix, split_recording

SESSION_RE = re.compile(r"^\d{8}-\d{6}$")
CLIP_RE = re.compile(r"^(?:[A-Za-z0-9-]{1,32}_)?\d{8}-\d{6}_\d{3}\.wav$")
LABELS = ("positive", "negative")
LOCK = threading.Lock()


def parse_params(raw: dict | None) -> SplitParams:
    raw = raw or {}
    return SplitParams(**{f.name: float(raw[f.name]) for f in fields(SplitParams) if raw.get(f.name) is not None})


def load_meta(path: Path) -> dict:
    meta_path = path / "clips.json"
    if meta_path.is_file():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return {"clips": [], "rejected": 0, "params": asdict(SplitParams())}


def write_meta(path: Path, meta: dict) -> None:
    (path / "clips.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def find_clip(meta: dict, name: str) -> dict:
    if not CLIP_RE.match(name or ""):
        raise ValueError("invalid clip name")
    for clip in meta["clips"]:
        if clip["name"] == name:
            return clip
    raise FileNotFoundError(f"clip not found: {name}")


class Studio:
    def __init__(self, project: Path):
        self.recordings = project / "recordings"
        self.targets = {label: project / "samples" / label for label in LABELS}

    def session_dir(self, name: str) -> Path:
        if not SESSION_RE.match(name or ""):
            raise ValueError(f"invalid session name: {name!r}")
        path = self.recordings / name
        if not (path / "recording.wav").is_file():
            raise FileNotFoundError(f"session not found: {name}")
        return path

    def clip_path(self, session: str, name: str) -> Path:
        if not CLIP_RE.match(name or ""):
            raise ValueError("invalid clip name")
        path = self.session_dir(session) / "clips" / name
        if not path.is_file():
            raise FileNotFoundError(f"clip not found: {name}")
        return path

    def clip_label(self, name: str) -> str | None:
        return next((label for label, target in self.targets.items() if (target / name).is_file()), None)

    def place_clip(self, path: Path, name: str, label: str | None) -> None:
        """Copy a clip into the set for its label and remove it from the other sets."""
        for target_label, target in self.targets.items():
            dest = target / name
            if target_label == label:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path / "clips" / name, dest)
            else:
                dest.unlink(missing_ok=True)

    def unplace_all(self, meta: dict) -> None:
        for clip in meta["clips"]:
            for target in self.targets.values():
                (target / clip["name"]).unlink(missing_ok=True)

    def session_info(self, path: Path) -> dict:
        meta = load_meta(path)
        clips_dir = path / "clips"
        meta["clips"] = [{**c, "label": self.clip_label(c["name"])} for c in meta["clips"] if (clips_dir / c["name"]).is_file()]
        meta["name"] = path.name
        meta["prefix"] = load_prefix(path)
        meta["source"] = load_session(path).get("source", "")
        meta["duration"] = round(sf.info(path / "recording.wav").duration, 1)
        return meta

    def list_sessions(self) -> list[dict]:
        if not self.recordings.is_dir():
            return []
        dirs = sorted(
            (d for d in self.recordings.iterdir() if SESSION_RE.match(d.name) and (d / "recording.wav").is_file()),
            reverse=True,
        )
        return [self.session_info(d) for d in dirs]

    def sample_counts(self) -> dict:
        return {label: len(list(target.glob("*.wav"))) if target.is_dir() else 0 for label, target in self.targets.items()}

    def new_session(self, wav: bytes, params: SplitParams, prefix: str, source: str = "") -> dict:
        """A recorded take, or an uploaded file (source: its name, shown in the take list)."""
        self.recordings.mkdir(parents=True, exist_ok=True)
        while True:
            path = self.recordings / datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                path.mkdir()
                break
            except FileExistsError:
                time.sleep(1)
        (path / "recording.wav").write_bytes(wav)
        if source:
            (path / "session.json").write_text(json.dumps({"source": source[:200]}, ensure_ascii=False), encoding="utf-8")
        return self.split(path, params, prefix)

    def split(self, path: Path, params: SplitParams, prefix: str | None = None) -> dict:
        self.unplace_all(load_meta(path))
        clips, rejected = split_recording(path / "recording.wav", params, prefix)
        write_meta(path, {"clips": clips, "rejected": rejected, "params": asdict(params)})
        return self.session_info(path)

    def rename(self, path: Path, prefix: str) -> dict:
        meta = load_meta(path)
        mapping = rename_prefix(path, prefix)
        for clip in meta["clips"]:
            new_name = mapping.get(clip["name"], clip["name"])
            if new_name != clip["name"]:
                for target in self.targets.values():
                    old_copy = target / clip["name"]
                    if old_copy.is_file():
                        old_copy.rename(old_copy.with_name(new_name))
            clip["name"] = new_name
        write_meta(path, meta)
        return self.session_info(path)

    def label(self, path: Path, names: list[str], value: str | None) -> dict:
        if value is not None and value not in self.targets:
            raise ValueError(f"invalid label: {value!r}")
        meta = load_meta(path)
        for name in names:
            find_clip(meta, name)
            self.place_clip(path, name, value)
        return self.session_info(path)

    def label_unsorted(self, path: Path, value: str) -> dict:
        names = [c["name"] for c in self.session_info(path)["clips"] if not c.get("label")]
        return self.label(path, names, value)

    def delete_clip(self, path: Path, name: str) -> dict:
        meta = load_meta(path)
        clip = find_clip(meta, name)
        self.place_clip(path, name, None)
        (path / "clips" / name).unlink(missing_ok=True)
        meta["clips"].remove(clip)
        write_meta(path, meta)
        return self.session_info(path)

    @staticmethod
    def delete_session(path: Path) -> None:
        shutil.rmtree(path)
