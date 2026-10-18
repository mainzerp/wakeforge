"""Everything a fresh machine needs below data/, downloaded from pinned sources and prepared (TF venv, CPU).

  py-train -m trainer.setup status
  py-train -m trainer.setup run --job <dir> [--only negatives rirs ...] [--speech-shards 3] [--keep-archives]
  py-train -m trainer.setup export <slug> [--out data/exports/<slug>.zip]
  py-train -m trainer.setup import <file.zip> [--slug new-slug]

Items (sources and conversions as in the old trainer's setup_* scripts, so the result is the same data):

  negatives  HF kahrendt/microwakeword: speech, dinner party, no speech, ambient eval as prebuilt features (zip)
  rirs       HF TaterTotterson/MIT_environmental_impulse_responses: 270 room impulse responses (16 kHz)
  audioset   HF agkphysics/AudioSet balanced train (10 tars of FLAC) -> 16 kHz mono WAV, background noise
  fma        Free Music Archive fma_small.zip (MP3) -> 16 kHz mono WAV, background music
  chime      CHiME-Home chime_home.tar.gz (48 kHz WAV) -> 16 kHz mono WAV, background rooms
  speech     HF MultiLlasa/Kartoffelphon-2.5M-de-ger test shards (Arrow): shard 0 is the voice bank source and
             33 h of German speech to mine; every further shard adds ~33 h for mining
  models     the pinned TTS, phone recognizer and Whisper weights (trainer/models.py), OmniVoice venv
  voices     the German voice bank built from speech shard 0 (trainer/tts/voices.py), OmniVoice venv, GPU

Downloads resume after an interruption (<downloads>/setup/<file>.part) and are checked against size and SHA-256.
Archives are streamed member by member, so the extra disk space is one archive at most (deleted afterwards).
An item is ready when its expected file count is present; `run` skips ready items and resumes partial ones.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Iterator

from trainer import jobs
from trainer.paths import DATA, DATASETS, DOWNLOADS, PROJECTS, VOICES

HF = "https://huggingface.co/datasets"
SCRATCH = DOWNLOADS / "setup"
SPEECH_DIR = DOWNLOADS / "kartoffelphon"
SPEECH_REPO, SPEECH_REV = "MultiLlasa/Kartoffelphon-2.5M-de-ger", "510091818dc18bb0f455152fca8b84dfe85cbef5"
SPEECH_SHARDS = [  # test/data-NNNNN-of-00011.arrow, stored as test-NNNNN.arrow
    (481613112, "c0a231c334dc42c0a29dddab6b5ef87e28baa772e04c9f0ba27f3283883720f1"),
    (486963504, "de095c3b08868623a1d0c2a78a20a0f568dbdc0c6999bc6f609a62ab1a9cd3d9"),
    (487094160, "b6f1366fdcb28f20027c059a8c4aa3ed0474520be5019a70f39148378a5a599b"),
    (488968904, "ed3b4dd9d36dea7fe71d2c402d0f61e5afabfc9306666bee32263d370768d211"),
    (482235776, "52efc0e263e3079fef79c15779ef31449ee3c88cbf321720d94df9df1949a299"),
    (481893392, "627ad92e38c4c6426780074d54f24fc96c4f276961d98e8607e1c04f2e76094e"),
    (484852528, "bbfcfc039151e2b4f66638ed16d798ca8258d9b9e70265c0057c81775061f57b"),
    (486271264, "6bdb9b7bc06d189a76c9d9377e52b45b1fc51154c69e96d23f4fdad9af7d20b0"),
    (487296136, "971cf8bb7d04cff83822f8cf39ff5c0d3805ecee30d5c97cd94ebeff0dceaa7a"),
    (487898760, "aea764830b132b063692eb1442ea338a43c1a19083f409787a196219e4bef66b"),
    (482567688, "afa5ca225ba37de3eb2c4b07236ea0cdad055e3beb76228f3114c445599212d2"),
]
NEG_REV = "0da95f94302ca2f4aae3b18fc6560fa6d2bba3d1"
NEGATIVES = {  # zip: (size, sha256, *.ninja files after extraction)
    "dinner_party": (444310142, "18a0885d595ced7faa73736a8680206f5ba6e80113ca3e6ce43130e510aac18f", 94),
    "dinner_party_eval": (82329019, "e08e23a5e654dd4415a1c41a4b6cd02d1f983c821512367ad937bac1aeed8299", 94),
    "no_speech": (2000317854, "722dbcc967275c64c9581ecb85729549830a9094ce605ee22b2ddab9dbabf3c8", 188),
    "speech": (3183001091, "68b6b811b347b8321169f9e75ea2b9be73245943b4470af9532ca288d9f1b3cc", 141),
}
RIR_REPO, RIR_REV, RIR_COUNT = "TaterTotterson/MIT_environmental_impulse_responses", "1b3e09d2b96396bcb43b4ab29578bd5c0ec83c9a", 270
AUDIOSET_REV = "6762f044d1c88619c7f2006486036192128fb07e"
AUDIOSET_TARS = [
    (2802626560, "b35c094f83ac10b6cdf27a59f929278ae2d39d66cebe3d1dd5e58e50655cdb06"),
    (2788321280, "44576f28c7a6cbe2f55c05fef5baad32947de784717e9e8a974402da7caca4c8"),
    (2778736640, "63ffbee719ec0759e16ca6c3319aa501994042d02b3996b759c624a4abd297ad"),
    (2781644800, "e791b31fd5ad685dd6278da8818f26626a6a4c90d8ee00820abf3687ab857ee5"),
    (2772848640, "9f99305df0fb64c1ff6a6f2d74f50faef163fdfdc000299ad074de86527186f9"),
    (2804664320, "fe45e29db7c146e55e37b8b63be641bacb403eebcde0a57866a49a9f925d7f7b"),
    (2803824640, "d4065ac7d5d53425c64ef590fbba2863d3bcf0d532730a8c2584ddc4f8a74a2f"),
    (2789519360, "d5422f2d8925359ea629671738862e92b54b58d781ee883aa07f242bae3ebea7"),
    (2737479680, "93849b0b552f4ece53ffea24c6dcecec7b84a56c8e3368bb1befc33f6eb69325"),
    (968540160, "9ab88550e77c8289acf71d19ae3f254fadad16f3cea305ef08a16949a928acf9"),
]
AUDIOSET_COUNT = 18683
FMA = ("https://os.unil.cloud.switch.ch/fma/fma_small.zip", 7679594875,
       "f923bbef327820456d50965c3c320c3c7b6dab8456449429fd78f7ec96a5d02c", 7997)
CHIME = ("https://archive.org/download/chime-home/chime_home.tar.gz", 4162513717,
         "96757783fc2d709dc545cc9b284f299ece75f27f63dec7cc2a7ceca430fdc1c0", 6137)
# Hashes of the non-HF archives were taken from the old trainer's downloads (identical sizes; the HF tars match HF).
VOICE_COUNT = 300
GB = 1e9


class Stopped(Exception):
    pass


@dataclass
class Item:
    id: str
    label: str
    purpose: str
    needed_for: list[str]
    license: str
    download: float  # bytes
    disk: float  # bytes when ready
    scratch: float = 0.0  # extra bytes while preparing (one archive)
    optional: bool = False
    files: list[str] = field(default_factory=list)  # what it produces, relative to data/


def count(path: Path, pattern: str) -> int:
    return sum(1 for _ in path.rglob(pattern)) if path.is_dir() else 0


def items(speech_shards: int = 1) -> list[Item]:
    n = max(1, min(speech_shards, len(SPEECH_SHARDS)))
    return [
        Item("negatives", "Negative speech and noise features", "speech, dinner party, music and noise the model must "
             "ignore, plus 5 h of rooms that calibrate the false alarm rate", ["training"], "see the microWakeWord dataset card",
             sum(v[0] for v in NEGATIVES.values()), 25.0 * GB, 3.2 * GB, files=["datasets/negative_datasets"]),
        Item("rirs", "Room impulse responses (MIT)", "make clips sound like they were spoken in real rooms", ["training"],
             "CC-BY 4.0", 8.4e6, 6e6, files=["datasets/mit_rirs_16k"]),
        Item("audioset", "AudioSet balanced (background noise)", "everyday sounds mixed under the clips", ["training"],
             "CC-BY 4.0", sum(t[0] for t in AUDIOSET_TARS), 5.6 * GB, 2.8 * GB, files=["datasets/audioset_16k"]),
        Item("fma", "Free Music Archive small (background music)", "music mixed under the clips", ["training"],
             "CC licenses per track", FMA[1], 7.2 * GB, FMA[1], files=["datasets/fma_16k"]),
        Item("chime", "CHiME-Home (background rooms)", "living room noise mixed under the clips", ["training"],
             "CC-BY-SA 4.0", CHIME[1], 0.8 * GB, CHIME[1], files=["datasets/chime_16k"]),
        Item("speech", "German speech (Kartoffelphon)",
             "the first shard is the voice bank source, every shard about 33 h of real speech to mine", ["generation", "mining"],
             "CC-BY 4.0", sum(s[0] for s in SPEECH_SHARDS[:n]), sum(s[0] for s in SPEECH_SHARDS[:n]),
             files=[f"downloads/kartoffelphon/test-{i:05d}.arrow" for i in range(n)]),
        Item("models", "TTS, phone recognizer and Whisper weights", "OmniVoice voice cloning, the phonetic check, "
             "transcripts", ["generation", "mining"], "OmniVoice weights CC-BY-NC 4.0, others Apache/MIT", 6.2 * GB,
             6.2 * GB, files=["cache/huggingface"]),
        Item("voices", "German voice bank", "300 reference voices for cloning, built from the first speech shard (GPU, ~10 min)",
             ["generation"], "CC-BY 4.0 (derived from Kartoffelphon)", 0, 6e7, files=["voices/de"]),
    ]


def state(item: Item) -> dict:
    """ready | partial | missing, with what is there."""
    have, want = 0, 1
    if item.id == "negatives":
        root = DATASETS / "negative_datasets"
        have, want = sum(count(root / k, "*.ninja") for k in NEGATIVES), sum(v[2] for v in NEGATIVES.values())
    elif item.id == "rirs":
        have, want = count(DATASETS / "mit_rirs_16k", "*.wav"), RIR_COUNT
    elif item.id == "audioset":
        have, want = count(DATASETS / "audioset_16k", "*.wav"), AUDIOSET_COUNT
    elif item.id == "fma":
        have, want = count(DATASETS / "fma_16k", "*.wav"), FMA[3]
    elif item.id == "chime":
        have, want = count(DATASETS / "chime_16k", "*.wav"), CHIME[3]
    elif item.id == "speech":
        want = len(item.files)
        have = sum(1 for i, f in enumerate(item.files) if (DATA / f).is_file() and (DATA / f).stat().st_size == SPEECH_SHARDS[i][0])
    elif item.id == "models":
        from trainer import models

        want = len(models.ALL)
        hub = Path(os.environ.get("HF_HOME", DATA / "cache" / "huggingface")) / "hub"
        have = sum(1 for repo, rev in models.ALL.values()
                   if (hub / f"models--{repo.replace('/', '--')}" / "snapshots" / rev).is_dir())
    elif item.id == "voices":
        bank = VOICES / "de" / "voices.json"
        have = len(json.loads(bank.read_text(encoding="utf-8"))) if bank.is_file() else 0
        want = VOICE_COUNT
    status = "ready" if have >= want else "partial" if have else "missing"
    return {"state": status, "have": have, "want": want}


def quick() -> dict[str, bool]:
    """Cheap readiness per use (a few stats, no file counting) for the guide and the top bar; status() is exact."""
    def some(path: Path, pattern: str = "*") -> bool:
        return path.is_dir() and next(path.glob(pattern), None) is not None

    neg = DATASETS / "negative_datasets"
    training = all(some(neg / k / "training" if k != "dinner_party_eval" else neg / k) for k in NEGATIVES) and all(
        some(DATASETS / d, "*.wav") for d in ("mit_rirs_16k", "audioset_16k", "fma_16k", "chime_16k"))
    speech = (SPEECH_DIR / "test-00000.arrow").is_file()
    return {"training": training, "generation": speech and (VOICES / "de" / "voices.json").is_file(), "mining": speech}


def status(speech_shards: int | None = None) -> dict:
    present = sum(1 for i in range(len(SPEECH_SHARDS)) if (SPEECH_DIR / f"test-{i:05d}.arrow").is_file())
    n = speech_shards or max(1, present)
    out = [{**item.__dict__, **state(item)} for item in items(n)]
    DATA.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(DATA)
    missing = [i for i in out if i["state"] != "ready"]
    return {"items": out, "speech_shards": {"have": present, "total": len(SPEECH_SHARDS), "hours_each": 33},
            "disk": {"free": du.free, "total": du.total},
            "needed": {"download": sum(i["download"] for i in missing),
                       "disk": sum(i["disk"] + i["scratch"] for i in missing)},
            "ready_for": {use: all(i["state"] == "ready" for i in out if use in i["needed_for"])
                          for use in ("training", "generation", "mining")}}


# ---- downloading -----------------------------------------------------------------------------------------------

def request(url: str, start: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": "Wakeforge-setup/1.0"}
    if start:
        headers["Range"] = f"bytes={start}-"
    token = os.environ.get("HF_TOKEN")
    if token and url.startswith("https://huggingface.co/"):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def fetch(url: str, dest: Path, size: int | None, sha256: str | None, on_bytes: Callable[[int], None],
          stop: Callable[[], bool]) -> Path:
    """Download with resume and verification; returns dest. on_bytes gets every new chunk's length."""
    if dest.is_file() and (size is None or dest.stat().st_size == size):
        on_bytes(dest.stat().st_size)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, 6):
        digest, have = hashlib.sha256(), part.stat().st_size if part.is_file() else 0
        if size and have > size:
            part.unlink()
            have = 0
        if have:  # hash what is already there
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 22), b""):
                    digest.update(chunk)
            on_bytes(have)
        try:
            if not size or have < size:
                with urllib.request.urlopen(request(url, have), timeout=60) as r, open(part, "ab" if have else "wb") as f:
                    if have and r.status != 206:  # the server ignored the range: start over
                        f.truncate(0)
                        on_bytes(-have)
                        digest, have = hashlib.sha256(), 0
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        if stop():
                            raise Stopped
                        f.write(chunk)
                        digest.update(chunk)
                        on_bytes(len(chunk))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            on_bytes(-(part.stat().st_size if part.is_file() else 0))
            if attempt == 5:
                raise RuntimeError(f"download failed: {url}: {e}") from e
            print(f"   retry {attempt}/4 for {dest.name}: {e}", flush=True)
            time.sleep(5 * attempt)
            continue
        got = part.stat().st_size
        if (size and got != size) or (sha256 and digest.hexdigest() != sha256):
            part.unlink()
            raise RuntimeError(f"{dest.name}: download corrupt (size {got}, sha256 {digest.hexdigest()[:12]}...)")
        os.replace(part, dest)
        return dest
    raise RuntimeError(f"download failed: {url}")


# ---- conversion (same as the old trainer: librosa to 16 kHz mono, int16 WAV) ---------------------------------

def _convert(job: tuple[bytes, str]) -> str | None:
    import librosa
    import numpy as np
    import scipy.io.wavfile

    data, out = job
    try:
        try:
            y, _ = librosa.load(io.BytesIO(data), sr=16000, mono=True)
        except Exception:  # noqa: BLE001
            # From a path librosa falls back to audioread/ffmpeg when libsndfile fails (a few FMA MP3s), as the old
            # trainer, which converted extracted files, did.
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=Path(out).suffix + ".src") as tmp:
                tmp.write(data)
                tmp.flush()
                y, _ = librosa.load(tmp.name, sr=16000, mono=True)
        if y.size == 0:
            raise ValueError("empty audio")
        tmp = out + ".tmp"
        scipy.io.wavfile.write(tmp, 16000, (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16))
        os.replace(tmp, out)
        return None
    except Exception as e:  # noqa: BLE001 - a broken file is logged and skipped, like the old trainer did
        return f"{out}: {e}"


def _init_pool() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def convert_members(members: Iterator[tuple[str, bytes]], out_dir: Path, pool: Pool, on_file: Callable[[], None],
                    stop: Callable[[], bool]) -> list[str]:
    """Convert in batches: Pool.imap would read the whole archive ahead into memory (it has no back pressure)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bad, batch = [], []

    def flush() -> None:
        for err in pool.imap_unordered(_convert, batch, chunksize=4):
            if err:
                bad.append(err)
            on_file()
        batch.clear()
        if stop():
            raise Stopped

    for name, data in members:
        if (out_dir / name).is_file():
            on_file()
            continue
        batch.append((data, str(out_dir / name)))
        if len(batch) >= 256:
            flush()
    flush()
    return bad


def tar_members(path: Path, suffix: str, name: Callable[[str], str]) -> Iterator[tuple[str, bytes]]:
    with tarfile.open(path, "r|*") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith(suffix):
                yield name(m.name), tar.extractfile(m).read()


def zip_members(path: Path, suffix: str, name: Callable[[str], str]) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not info.is_dir() and info.filename.endswith(suffix):
                yield name(info.filename), z.read(info)


# ---- the job ------------------------------------------------------------------------------------------------------

class Setup:
    def __init__(self, args: argparse.Namespace):
        self.args, self.job = args, args.job
        self.stop = False
        self.child: subprocess.Popen | None = None
        self.items = items(args.speech_shards)
        self.todo = [i for i in self.items if (not args.only or i.id in args.only) and state(i)["state"] != "ready"]
        self.progress = {"state": "running", "phase": "setup", "items": {i.id: {"state": "waiting", "label": i.label,
                         "bytes": 0, "total": i.download, "files": 0} for i in self.todo},
                         "done_bytes": 0, "total_bytes": sum(i.download for i in self.todo), "rate": 0, "eta": None}
        self.t0, self.last = time.perf_counter(), 0.0
        signal.signal(signal.SIGTERM, self.on_term)
        signal.signal(signal.SIGINT, self.on_term)

    def on_term(self, *_):
        self.stop = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def stopped(self) -> bool:
        return self.stop

    def write(self, force: bool = False, **update) -> None:
        self.progress.update(update)
        now = time.perf_counter()
        if not force and now - self.last < 1.0:
            return
        self.last = now
        done = self.progress["done_bytes"]
        rate = done / max(1e-6, now - self.t0)
        self.progress.update(rate=round(rate), eta=round((self.progress["total_bytes"] - done) / rate) if rate > 1e5 else None,
                             updated=time.time())
        jobs.write_progress_file(self.job, self.progress)

    def item(self, item_id: str, **update) -> None:
        self.progress["items"][item_id].update(update)
        self.write(force="state" in update)

    def counter(self, item_id: str) -> Callable[[int], None]:
        def on_bytes(n: int) -> None:
            self.progress["items"][item_id]["bytes"] += n
            self.progress["done_bytes"] += n
            self.write()
        return on_bytes

    def file_counter(self, item_id: str) -> Callable[[], None]:
        def on_file() -> None:
            self.progress["items"][item_id]["files"] += 1
            self.write()
        return on_file

    def check_disk(self) -> None:
        need = sum(i.disk + i.scratch for i in self.todo)
        free = shutil.disk_usage(DATA).free
        if need > free:
            raise RuntimeError(f"not enough disk space below {DATA}: about {need / GB:.0f} GB needed, {free / GB:.0f} GB free")

    def run(self) -> None:
        self.check_disk()
        self.write(force=True, message=f"{len(self.todo)} items to prepare")
        with Pool(max(1, min(12, (os.cpu_count() or 2) - 2)), initializer=_init_pool) as pool:
            for item in self.todo:
                if self.stop:
                    break
                print(f"== {item.id}: {item.label}", flush=True)
                self.item(item.id, state="running")
                try:
                    getattr(self, f"do_{item.id}")(item, pool)
                except Stopped:
                    break
                ok = state(item)["state"] == "ready"
                self.item(item.id, state="done" if ok else "incomplete")
                print(f"   {item.id}: {'ready' if ok else 'incomplete, see above'}", flush=True)
        final = "stopped" if self.stop else "done" if all(state(i)["state"] == "ready" for i in self.todo) else "failed"
        self.write(force=True, state=final, eta=0,
                   message={"stopped": "stopped; start again to resume", "done": "all data is ready",
                            "failed": "some items are incomplete, see the log"}[final])

    def archive(self, name: str) -> Path:
        return SCRATCH / name

    def cleanup(self, path: Path) -> None:
        if not self.args.keep_archives:
            path.unlink(missing_ok=True)

    def do_negatives(self, item: Item, pool: Pool) -> None:
        root = DATASETS / "negative_datasets"
        for name, (size, sha, files) in NEGATIVES.items():
            if count(root / name, "*.ninja") == files:
                self.counter(item.id)(size)
                continue
            z = fetch(f"{HF}/kahrendt/microwakeword/resolve/{NEG_REV}/{name}.zip", self.archive(f"{name}.zip"), size, sha,
                      self.counter(item.id), self.stopped)
            self.item(item.id, message=f"unpacking {name}")
            shutil.rmtree(root / name, ignore_errors=True)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(root)
            self.cleanup(z)

    def do_rirs(self, item: Item, pool: Pool) -> None:
        import numpy as np
        import scipy.io.wavfile
        import soundfile as sf

        with urllib.request.urlopen(request(f"https://huggingface.co/api/datasets/{RIR_REPO}/tree/{RIR_REV}/16khz"), timeout=60) as r:
            files = [f for f in json.loads(r.read()) if f["path"].lower().endswith(".wav")]
        out = DATASETS / "mit_rirs_16k"
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            name = Path(f["path"]).name
            if (out / name).is_file():
                self.counter(item.id)(f["size"])
                continue
            raw = fetch(f"{HF}/{RIR_REPO}/resolve/{RIR_REV}/{f['path']}", self.archive(f"rir-{name}"), f["size"], None,
                        self.counter(item.id), self.stopped)
            a, sr = sf.read(raw, always_2d=True)
            if sr != 16000:
                import librosa

                a, sr = librosa.load(raw, sr=16000, mono=True), 16000
                a = a[0]
            else:
                a = a[:, 0]
            scipy.io.wavfile.write(out / name, 16000, (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16))
            raw.unlink()

    def do_audioset(self, item: Item, pool: Pool) -> None:
        out = DATASETS / "audioset_16k"
        bad = []
        for i, (size, sha) in enumerate(AUDIOSET_TARS):
            tar = fetch(f"{HF}/agkphysics/AudioSet/resolve/{AUDIOSET_REV}/data/bal_train0{i}.tar",
                        self.archive(f"bal_train0{i}.tar"), size, sha, self.counter(item.id), self.stopped)
            self.item(item.id, message=f"converting bal_train0{i}.tar")
            bad += convert_members(tar_members(tar, ".flac", lambda n: Path(n).stem + ".wav"), out, pool,
                                   self.file_counter(item.id), self.stopped)
            self.cleanup(tar)
        if bad:
            (out / "audioset_corrupted_files.log").write_text("\n".join(bad), encoding="utf-8")

    def do_fma(self, item: Item, pool: Pool) -> None:
        url, size, sha, _ = FMA
        z = fetch(url, self.archive("fma_small.zip"), size, sha, self.counter(item.id), self.stopped)
        self.item(item.id, message="converting fma_small.zip")
        out = DATASETS / "fma_16k"
        bad = convert_members(zip_members(z, ".mp3", lambda n: Path(n).stem + ".wav"), out, pool,
                              self.file_counter(item.id), self.stopped)
        if bad:
            (out / "fma_corrupted_files.log").write_text("\n".join(bad), encoding="utf-8")
        self.cleanup(z)

    def do_chime(self, item: Item, pool: Pool) -> None:
        url, size, sha, _ = CHIME
        tar = fetch(url, self.archive("chime_home.tar.gz"), size, sha, self.counter(item.id), self.stopped)
        self.item(item.id, message="converting chime_home.tar.gz")
        # The old trainer extracted the tar and named each file by its path: chime_home__chunks__<name>.48kHz.wav
        bad = convert_members(tar_members(tar, ".48kHz.wav", lambda n: "__".join(Path(n).parts)), DATASETS / "chime_16k",
                              pool, self.file_counter(item.id), self.stopped)
        if bad:
            (DATASETS / "chime_16k" / "chime_corrupted_files.log").write_text("\n".join(bad), encoding="utf-8")
        self.cleanup(tar)

    def do_speech(self, item: Item, pool: Pool) -> None:
        for i, (size, sha) in enumerate(SPEECH_SHARDS[:len(item.files)]):
            fetch(f"{HF}/{SPEECH_REPO}/resolve/{SPEECH_REV}/test/data-{i:05d}-of-{len(SPEECH_SHARDS):05d}.arrow",
                  SPEECH_DIR / f"test-{i:05d}.arrow", size, sha, self.counter(item.id), self.stopped)

    def omni(self, item: Item, args: list[str]) -> None:
        exe = shutil.which("py-omni")
        if not exe:
            raise RuntimeError("the py-omni launcher is missing (run the setup inside the container)")
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}  # each venv brings its own CUDA libs
        self.child = subprocess.Popen([exe, *args], env=env)
        code = self.child.wait()
        self.child = None
        if code and not self.stop:
            raise RuntimeError(f"{item.id} failed (exit {code}), see the log")

    def do_models(self, item: Item, pool: Pool) -> None:
        self.item(item.id, message="downloading the model weights (about 6 GB)")
        self.omni(item, ["-c", "from trainer import models\nfor m in models.ALL.values(): print(models.local(m), flush=True)"])
        self.counter(item.id)(int(item.download))

    def do_voices(self, item: Item, pool: Pool) -> None:
        self.item(item.id, message="building the voice bank on the GPU")
        self.omni(item, ["-m", "trainer.tts.voices", "build", "--language", "de", "--max", str(VOICE_COUNT)])


# ---- moving projects between machines ---------------------------------------------------------------------------

SKIP = {"jobs"}  # job logs and pids only mean something on the machine that ran them


def export_project(slug: str, out: Path | None = None) -> Path:
    src = PROJECTS / slug
    if not (src / "project.json").is_file():
        raise FileNotFoundError(f"project not found: {slug}")
    out = out or DATA / "exports" / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:  # audio does not compress
        for f in sorted(src.rglob("*")):
            rel = f.relative_to(src)
            if f.is_file() and rel.parts[0] not in SKIP and not f.name.endswith((".tmp", ".part")):
                z.write(f, f"{slug}/{rel.as_posix()}")
    os.replace(tmp, out)
    return out


def import_project(archive: Path, slug: str | None = None) -> str:
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if len(roots) != 1 or f"{next(iter(roots))}/project.json" not in names:
            raise ValueError("not a project export: expected one folder with a project.json")
        src = next(iter(roots))
        slug = slug or src
        from trainer.projects import SLUG_RE

        if not SLUG_RE.match(slug):
            raise ValueError(f"invalid project name: {slug!r}")
        dest = PROJECTS / slug
        if dest.exists():
            raise ValueError(f"project {slug!r} exists already; pass another slug")
        tmp = PROJECTS / f".import-{slug}"
        shutil.rmtree(tmp, ignore_errors=True)
        for n in names:
            if n.endswith("/") or ".." in Path(n).parts:
                continue
            target = tmp / Path(n).relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as r, open(target, "wb") as w:
                shutil.copyfileobj(r, w, 1 << 20)
    os.replace(tmp, dest)
    return slug


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("--speech-shards", type=int)
    r = sub.add_parser("run")
    r.add_argument("--job", type=Path, required=True)
    r.add_argument("--only", nargs="+", help="item ids (default: everything that is not ready)")
    r.add_argument("--speech-shards", type=int, default=1, help="Kartoffelphon shards, ~33 h of speech each (1-11)")
    r.add_argument("--keep-archives", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("slug")
    e.add_argument("--out", type=Path)
    i = sub.add_parser("import")
    i.add_argument("archive", type=Path)
    i.add_argument("--slug")
    args = ap.parse_args()
    if args.command == "status":
        s = status(args.speech_shards)
        for it in s["items"]:
            print(f"{it['id']:10} {it['state']:8} {it['have']:>6}/{it['want']:<6} {it['download'] / GB:6.1f} GB  {it['label']}")
        print(f"ready for: {', '.join(k for k, v in s['ready_for'].items() if v) or 'nothing yet'}; "
              f"missing: {s['needed']['download'] / GB:.1f} GB to download, {s['disk']['free'] / GB:.0f} GB free")
    elif args.command == "run":
        args.job.mkdir(parents=True, exist_ok=True)
        setup = Setup(args)
        try:
            setup.run()
        except Exception as ex:
            setup.write(force=True, state="failed", message=f"{type(ex).__name__}: {ex}")
            raise
    elif args.command == "export":
        print(export_project(args.slug, args.out))
    else:
        print(import_project(args.archive, args.slug))


if __name__ == "__main__":
    main()
