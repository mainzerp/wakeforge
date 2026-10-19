"""Energy / noise-floor based splitter for long takes of repeated wake words."""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import percentile_filter
from scipy.signal import resample_poly

TARGET_RATE = 16000
FRAME_S = 0.02
HOP_S = 0.01
PREFIX_MAX_LEN = 32
UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
CLIP_SUFFIX_RE = re.compile(r"(\d{8}-\d{6})_(\d{3})\.wav$")
# A take this short is one clip as a whole: the noise floor of a single word cannot be estimated (the splitter
# dropped or clipped 4 of 10 one-word files), and uploaded files are often exactly that.
SINGLE_CLIP_MAX_S = 3.0


@dataclass
class SplitParams:
    margin_db: float = 12.0
    min_len: float = 0.3
    max_len: float = 2.0
    merge_gap: float = 0.2
    pad: float = 0.15
    floor_window: float = 5.0


def sanitize_prefix(text: str | None) -> str:
    """Reduce free text to a filename-safe prefix of letters, digits and dashes."""
    ascii_text = unicodedata.normalize("NFKD", (text or "").translate(UMLAUTS)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-")[:PREFIX_MAX_LEN].strip("-")


def clip_name(session: str, index: int, prefix: str) -> str:
    return f"{prefix}_{session}_{index:03d}.wav" if prefix else f"{session}_{index:03d}.wav"


def load_prefix(session_dir: Path) -> str:
    path = session_dir / "session.json"
    return json.loads(path.read_text(encoding="utf-8")).get("prefix", "") if path.is_file() else ""


def load_session(session_dir: Path) -> dict:
    path = session_dir / "session.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_prefix(session_dir: Path, prefix: str) -> None:
    """Store the prefix, keeping the other keys of session.json (the uploaded file's name)."""
    data = {**load_session(session_dir), "prefix": prefix}
    (session_dir / "session.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def rename_prefix(session_dir: Path, prefix: str) -> dict[str, str]:
    """Rename existing clips to a new prefix and return the old -> new name mapping."""
    prefix = sanitize_prefix(prefix)
    mapping = {}
    for clip in sorted((session_dir / "clips").glob("*.wav")):
        match = CLIP_SUFFIX_RE.search(clip.name)
        if not match:
            continue
        new_name = clip_name(match.group(1), int(match.group(2)), prefix)
        if new_name != clip.name:
            clip.rename(clip.with_name(new_name))
        mapping[clip.name] = new_name
    save_prefix(session_dir, prefix)
    return mapping


def frame_db(audio: np.ndarray, rate: int) -> np.ndarray:
    frame, hop = int(FRAME_S * rate), int(HOP_S * rate)
    n = max(0, 1 + (len(audio) - frame) // hop)
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(audio[idx] ** 2, axis=1))
    return 20 * np.log10(rms + 1e-12)


def regions(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def find_segments(audio: np.ndarray, rate: int, p: SplitParams) -> tuple[list[tuple[float, float]], int]:
    """Return word segments in seconds and the number of rejected regions."""
    db = frame_db(audio, rate)
    floor = percentile_filter(db, 10, size=max(3, int(p.floor_window / HOP_S)), mode="nearest")
    core = db > floor + p.margin_db
    loose = db > floor + p.margin_db / 2

    spans = [(s, e) for s, e in regions(loose) if core[s:e].any()]
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and (s - merged[-1][1]) * HOP_S < p.merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    segments, rejected = [], 0
    for s, e in merged:
        start, end = s * HOP_S, e * HOP_S + FRAME_S
        if p.min_len <= end - start <= p.max_len:
            segments.append((start, end))
        else:
            rejected += 1
    return segments, rejected


def split_recording(wav_path: Path, p: SplitParams, prefix: str | None = None) -> tuple[list[dict], int]:
    """Write clips to <session>/clips and return their metadata and the rejected count.

    A prefix of None keeps the prefix stored for the session.
    """
    if prefix is None:
        prefix = load_prefix(wav_path.parent)
    prefix = sanitize_prefix(prefix)
    save_prefix(wav_path.parent, prefix)
    audio, rate = sf.read(wav_path, dtype="float32", always_2d=True)
    audio = audio[:, 0]
    if rate != TARGET_RATE:
        g = np.gcd(rate, TARGET_RATE)
        audio = resample_poly(audio, TARGET_RATE // g, rate // g).astype(np.float32)
        rate = TARGET_RATE

    total = len(audio) / rate
    segments, rejected = ([(0.0, total)], 0) if total <= SINGLE_CLIP_MAX_S else find_segments(audio, rate, p)
    clips_dir = wav_path.parent / "clips"
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir()

    session = wav_path.parent.name
    clips = []
    for i, (start, end) in enumerate(segments):
        prev_end = segments[i - 1][1] if i else 0.0
        next_start = segments[i + 1][0] if i + 1 < len(segments) else total
        a = max(start - p.pad, (prev_end + start) / 2, 0.0)
        b = min(end + p.pad, (end + next_start) / 2, total)
        clip = audio[int(a * rate):int(b * rate)]
        peak = float(20 * np.log10(np.max(np.abs(clip)) + 1e-12))
        name = clip_name(session, i + 1, prefix)
        sf.write(clips_dir / name, clip, rate, subtype="PCM_16")
        clips.append({
            "name": name,
            "start": round(start, 2),
            "length": round(end - start, 2),
            "clip_start": round(a, 3),
            "clip_end": round(b, 3),
            "peak_db": round(peak, 1),
        })
    return clips, rejected
