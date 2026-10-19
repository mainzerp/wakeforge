"""Mining runs of a project and their review (no TF; used by the server, the scan job and the triage job).

  <project>/mining/<YYYYMMDD-HHMMSS>/mine.json     model, cutoffs, sources, hours scanned, state (written by the job)
  <project>/mining/<YYYYMMDD-HHMMSS>/hits.json     every trigger: source, position, score, transcript (job)
  <project>/mining/<YYYYMMDD-HHMMSS>/clips/<n>.wav context around a trigger, CONTEXT_BEFORE + CONTEXT_AFTER s (job)
  <project>/mining/<YYYYMMDD-HHMMSS>/triage.json   automatic check per trigger: phones, text, verdict (triage.py)
  <project>/mining/<YYYYMMDD-HHMMSS>/labels.json   decisions {n: negative|positive|skip} (server, triage)
  <project>/mining/<YYYYMMDD-HHMMSS>/auto.json     the labels the triage set and nobody changed since
  <project>/mining/negatives/<id>_<n>.wav          false triggers, a training source (trainer/train/sources.py)
  <project>/mining/positives/<id>_<n>.wav          triggers that really were the wake word, a training source

A labeled clip is cut from the context: CUT_BEFORE s before the trigger to CUT_AFTER s after it, which covers the
model's receptive field (~1.2 s) plus the sliding window. Labels live apart from hits.json, so a run can be reviewed
while the job is still writing; a file lock serializes the server and the triage process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import soundfile as sf

try:
    import fcntl
except ImportError:  # host run on Windows: the server is the only writer there
    fcntl = None

MINE_RE = re.compile(r"^\d{8}-\d{6}$")
LABELS = ("negative", "positive", "skip")
CONTEXT_BEFORE = 3.5
CONTEXT_AFTER = 1.0
CUT_BEFORE = 2.0
CUT_AFTER = 0.3
_THREAD_LOCK = threading.Lock()


def root(project: Path) -> Path:
    return project / "mining"


def negatives_dir(project: Path) -> Path:
    return root(project) / "negatives"


def positives_dir(project: Path) -> Path:
    return root(project) / "positives"


def label_dir(project: Path, label: str) -> Path | None:
    return {"negative": negatives_dir(project), "positive": positives_dir(project)}.get(label)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked(path: Path):
    with _THREAD_LOCK:
        if fcntl is None:
            yield
            return
        with open(path / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def mine_dirs(project: Path) -> list[Path]:
    r = root(project)
    return sorted((d for d in r.iterdir() if MINE_RE.match(d.name)), reverse=True) if r.is_dir() else []


def mine_dir(project: Path, mine_id: str) -> Path:
    if not MINE_RE.match(mine_id or ""):
        raise ValueError(f"invalid mining run: {mine_id!r}")
    path = root(project) / mine_id
    if not (path / "mine.json").is_file():
        raise FileNotFoundError(f"mining run not found: {mine_id}")
    return path


def hits(path: Path) -> list[dict]:
    return read_json(path / "hits.json", [])


def labels(path: Path) -> dict[str, str]:
    return read_json(path / "labels.json", {})


def auto(path: Path) -> set[str]:
    return set(read_json(path / "auto.json", []))


def triage(path: Path) -> dict[str, dict]:
    return read_json(path / "triage.json", {})


def summary(path: Path) -> dict:
    meta = read_json(path / "mine.json", {})
    hs, ls, au, tr = hits(path), labels(path), auto(path), triage(path)
    counts = {label: sum(1 for v in ls.values() if v == label) for label in LABELS}
    counts["open"] = len(hs) - sum(1 for h in hs if str(h["n"]) in ls)
    counts["auto"] = len(au)
    counts["flagged"] = sum(1 for t in tr.values() if t.get("verdict") == "review")
    device = sum(1 for h in hs if h["score"] > meta.get("cutoff", 1.0))
    hours = meta.get("hours_done", 0.0)
    return {**meta, "hits": len(hs), "counts": counts, "matches": sum(1 for h in hs if h.get("match")),
            "triaged": len(tr), "device_hits": device, "faph": round(device / hours, 2) if hours else None}


def clip_name(mine_id: str, n: int) -> str:
    return f"{mine_id}_{n:05d}.wav"


def set_labels(project: Path, mine_id: str, ns: list[int], label: str | None, by_triage: bool = False) -> dict:
    """Label hits (None clears the label): writes the training cut into the negative/positive set or removes it.

    by_triage: the automatic check labels; it never overrides a decision a person made (labeled and not in auto.json).
    """
    if label is not None and label not in LABELS:
        raise ValueError(f"invalid label: {label!r}")
    path = mine_dir(project, mine_id)
    by_n = {h["n"]: h for h in hits(path)}
    with locked(path):
        ls, au = labels(path), auto(path)
        for n in ns:
            hit, key = by_n.get(n), str(n)
            if hit is None:
                raise FileNotFoundError(f"hit not found: {n}")
            if by_triage and key in ls and key not in au:
                continue
            name = clip_name(mine_id, n)
            for other in ("negative", "positive"):
                if other != label:
                    (label_dir(project, other) / name).unlink(missing_ok=True)
            if by_triage and label is not None:
                au.add(key)
            else:
                au.discard(key)
            if label is None:
                ls.pop(key, None)
                continue
            target = label_dir(project, label)
            if target is not None and not (target / name).is_file():
                audio, sr = sf.read(path / "clips" / f"{n:05d}.wav", dtype="int16")
                a = max(0, int((hit["pre"] - CUT_BEFORE) * sr))
                b = int((hit["pre"] + CUT_AFTER) * sr)
                target.mkdir(parents=True, exist_ok=True)
                tmp = target / f".{name}"
                sf.write(tmp, audio[a:b], sr, subtype="PCM_16", format="WAV")
                os.replace(tmp, target / name)
            ls[key] = label
        write_json(path / "labels.json", ls)
        write_json(path / "auto.json", sorted(au, key=int))
    return summary(path)


def cut(path: Path, hit: dict):
    """The training cut of a hit as float32 at 16 kHz."""
    audio, sr = sf.read(path / "clips" / f"{hit['n']:05d}.wav", dtype="float32")
    return audio[max(0, int((hit["pre"] - CUT_BEFORE) * sr)):int((hit["pre"] + CUT_AFTER) * sr)]


def delete(project: Path, mine_id: str) -> None:
    """Remove a mining run; clips already labeled stay in the training sets."""
    shutil.rmtree(mine_dir(project, mine_id))


def rounds(project: Path) -> int:
    """Mining rounds so far: training runs with at least one finished scan."""
    return len({read_json(d / "mine.json", {}).get("run") for d in mine_dirs(project)
                if read_json(d / "mine.json", {}).get("state") == "done"} - {None})


def totals(project: Path) -> dict:
    count = lambda d: sum(1 for _ in d.glob("*.wav")) if d.is_dir() else 0  # noqa: E731
    return {"negatives": count(negatives_dir(project)), "positives": count(positives_dir(project))}
