"""What goes into a training run: the feature sources of a project with their weights.

Every clip set we featurize ourselves is split into cacheable items: one per synthetic corpus shard (shards are
immutable once written) and one per WAV folder. An item's key covers its content and the featurization settings, so
features are reused across runs and projects (a borrowed project shares the cache) and new shards or recordings only
featurize what is new. The shared negative datasets from the old trainer are already featurized.

Each clip lands in training/validation/testing by a hash of its name (80/10/10), stable across runs.

Pure Python (no TF): the server lists the sources for the UI, the worker featurizes and trains them.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from trainer import projects, recipe
from trainer.paths import DATASETS, FEATURE_CACHE, PROJECTS
from trainer.tts import corpus

# Bump when augmentation or spectrogram storage changes: every cached feature set is rebuilt.
FEATURE_VERSION = "f1-aug3.2-rep2x10-crop178-u16"
SPLIT_SALT = "split-v1"


@dataclass
class Item:
    kind: str  # shard | wavs
    path: str
    key: str
    clips: int

    @property
    def cache(self) -> Path:
        return FEATURE_CACHE / self.key


@dataclass
class Source:
    id: str
    label: str
    truth: bool
    sampling_weight: float
    penalty_weight: float = 1.0
    truncation: str = "truncate_start"
    items: list[Item] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # prebuilt feature dirs (shared datasets)
    clips: int = 0
    note: str = ""

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("items")
        d.pop("dirs")
        d["cached"] = sum(1 for i in self.items if (i.cache / "done").is_file())
        d["parts"] = len(self.items)
        d["prebuilt"] = bool(self.dirs)
        return d


def split_of(name: str) -> str:
    h = int(hashlib.sha1(f"{SPLIT_SALT}:{name}".encode()).hexdigest()[:8], 16) % 10
    return "testing" if h == 0 else "validation" if h == 1 else "training"


def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in (FEATURE_VERSION, *parts)).encode()).hexdigest()[:20]


def shard_items(path: Path, keys: bool = True) -> list[Item]:
    """keys=False: counts from the index only, no stat per shard (a stat costs ~3 ms on the Windows bind mount)."""
    index = corpus.load_index(path)
    if not keys:
        return [Item("shard", str(path / s["name"]), "", s["count"]) for s in index["shards"]]
    out = []
    for s in index["shards"]:
        f = path / s["name"]
        if f.is_file():
            st = f.stat()
            out.append(Item("shard", str(f), _key("shard", f.resolve(), st.st_size, st.st_mtime_ns), s["count"]))
    return out


def wav_item(path: Path, keys: bool = True) -> list[Item]:
    files = sorted(path.glob("*.wav")) if path.is_dir() else []
    if not files:
        return []
    if not keys:
        return [Item("wavs", str(path), "", len(files))]
    sig = [(f.name, st.st_size, st.st_mtime_ns) for f in files for st in (f.stat(),)]
    return [Item("wavs", str(path), _key("wavs", path.resolve(), sig), len(files))]


@lru_cache(maxsize=64)
def mmap_len(path: Path) -> int:
    """Number of spectrograms in a RaggedMmap (memory-mapped, cheap)."""
    from mmap_ninja.ragged import RaggedMmap

    return len(RaggedMmap(str(path)))


def shared_sources() -> list[Source]:
    neg = DATASETS / "negative_datasets"
    specs = [
        ("speech", "Speech (VOiCES)", 12.0, "random"),
        ("dinner_party", "Dinner party (CHiME-6)", 12.0, "random"),
        ("no_speech", "No speech (FMA, FSD50K, WHAM)", 5.0, "random"),
        ("dinner_party_eval", "Ambient eval (CHiME-6 dev, DiPCo)", 0.0, "split"),
    ]
    out = []
    for name, label, weight, trunc in specs:
        d = neg / name
        if d.is_dir():
            n = sum(mmap_len(m) for m in d.glob("*/*_mmap"))
            out.append(Source(f"shared:{name}", label, False, weight, 1.0, trunc, dirs=[str(d)], clips=n,
                              note="validation and test ambient audio" if weight == 0 else "shared, prebuilt"))
    return out


def project_sources(path: Path, keys: bool = True) -> list[Source]:
    """keys=False only counts clips (no cache keys, no stat per file): enough for overviews like the guide."""
    data = projects.load(path)
    shards = lambda d: shard_items(d, keys)  # noqa: E731
    wavs = lambda d: wav_item(d, keys)  # noqa: E731
    out = [
        Source("synthetic", "Synthetic positives", True, 2.0, items=shards(corpus.corpus_dir(path))),
        Source("own-positive", "Own recordings: wake word", True, 3.0, items=wavs(path / "samples" / "positive")),
        Source("sound-alikes", "Sound-alike negatives", False, 6.0, 1.25, items=shards(corpus.confusable_dir(path))),
        Source("own-negative", "Own recordings: not the wake word", False, 8.0, 1.25,
               items=wavs(path / "samples" / "negative")),
        Source("mined-negative", "Mined false triggers", False, 8.0, 1.25, items=wavs(path / "mining" / "negatives"),
               note="reviewed in the mining step"),
        Source("mined-positive", "Mined: wake word in real speech", True, 3.0, items=wavs(path / "mining" / "positives"),
               note="reviewed in the mining step"),
    ]
    for b in projects.borrowed_sources(data):
        other = PROJECTS / b["slug"]
        if b["kind"] == "recordings":
            items = [i for d in b["dirs"] for i in wavs(Path(d))]
        else:
            items = shards(corpus.corpus_dir(other))
        out.append(Source(f"borrowed:{b['slug']}:{b['kind']}", f"Borrowed from {b['slug']} ({b['kind']})", False,
                          4.0, items=items))
    for s in out:
        s.clips = sum(i.clips for i in s.items)
    return [s for s in out if s.clips] + shared_sources()


def advice(sources: list[Source]) -> list[dict]:
    """Amounts below what a model needs to hold up (trainer/recipe.py): shown before training, never blocking,
    so a quick fast run stays possible. Templates with params, translated by the UI."""
    clips = {s.id: s.clips for s in sources}
    out = []
    if clips.get("synthetic", 0) < recipe.MIN_POSITIVES:
        out.append({"text": "{n} synthetic samples: models hold up from {min} on.",
                    "params": {"n": clips.get("synthetic", 0), "min": recipe.MIN_POSITIVES}})
    if clips.get("sound-alikes", 0) < recipe.MIN_SOUND_ALIKES:
        out.append({"text": "{n} sound-alike clips: {min} or more teach the model to ignore similar words.",
                    "params": {"n": clips.get("sound-alikes", 0), "min": recipe.MIN_SOUND_ALIKES}})
    return out


def check(sources: list[Source]) -> list[str]:
    """Reasons a run cannot start."""
    problems = []
    if not any(s.truth and s.clips for s in sources):
        problems.append("no positives: generate synthetic samples or record some first")
    if not any(s.id == "shared:dinner_party_eval" for s in sources):
        problems.append("the ambient evaluation set (datasets/negative_datasets/dinner_party_eval) is missing: "
                        "download the shared data on the Data page (top bar)")
    if not any(s.id.startswith("shared:") and s.sampling_weight > 0 for s in sources):
        problems.append("no shared negative datasets found below datasets/negative_datasets: "
                        "download the shared data on the Data page (top bar)")
    return problems
