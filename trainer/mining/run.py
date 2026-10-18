"""Mining job: stream a trained model over hours of audio and collect every trigger for review (TF venv, CPU only).

  py-train -m trainer.mining.run --project data/projects/x --run <training run id> --sources kartoffelphon --job <dir>

Runs on the CPU on purpose: the quantized streaming TFLite model sees the audio exactly like ESPHome on the device
(microfrontend features, 2 slices per invoke, sliding-window average, cooldown after an accept as in calibrate.py).
No TensorFlow import: features come from pymicro_features, inference from ai_edge_litert, ~370x realtime per core.
Each task is one continuous stream (frontend and model state run through it, see audio.py); triggers in the first
WARMUP_S of a stream are ignored, the frontend's noise estimate still settles there.

A trigger is an accept at --cutoff (default: the run's calibrated cutoff; lower it to also collect near misses).
Its score is the highest window average within the cooldown, so the UI can tell which triggers the device cutoff
would have fired on. Output: <project>/mining/<YYYYMMDD-HHMMSS>/ (see trainer/mining/store.py).
After the scan the job runs the automatic check (trainer/mining/triage.py, OmniVoice venv on the GPU) as a child:
it labels the clear false triggers and leaves the suspicious ones for review. --no-auto: the audio may contain the
wake word (own rooms), so nothing is labeled automatically.
SIGTERM stops after the tasks in flight (it reaches the check too); the hits so far stay reviewable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import soundfile as sf

from trainer import jobs, projects
from trainer.mining import audio, store
from trainer.train.calibrate import COOLDOWN, moving_average

SR = audio.SAMPLE_RATE
STEP = 160  # 10 ms feature step
WINDOW = 480  # 30 ms feature window
MAX_HITS = 5000
WARMUP_S = 1.5

_detector = None
_cfg: dict = {}


def write_progress(job: Path, **data) -> None:
    data["updated"] = time.time()
    jobs.write_progress_file(job, data)


def pcm16(samples: np.ndarray) -> bytes:
    return np.clip(samples * 32768, -32768, 32767).astype(np.int16).tobytes()


def features(samples: np.ndarray) -> np.ndarray:
    """microWakeWord's generate_features_for_clip (C frontend), without importing TensorFlow."""
    from pymicro_features import MicroFrontend

    pcm = pcm16(samples)
    frontend, out, i = MicroFrontend(), [], 0
    while i + STEP * 2 < len(pcm):
        r = frontend.process_samples(pcm[i:i + STEP * 2])
        i += r.samples_read * 2
        if r.features:
            out.append(r.features)
    return np.asarray(out, dtype=np.float32).reshape(-1, 40)


class Detector:
    """A streaming TFLite model; its internal state carries over between calls like on the device.

    predict() scores one clip with a fresh frontend (as microwakeword.inference does); start() + feed() keep the
    frontend running across calls, so a stream can be fed in blocks."""

    def __init__(self, model: str):
        from ai_edge_litert.interpreter import Interpreter

        self.it = Interpreter(model_path=model, num_threads=1)
        self.it.allocate_tensors()
        inputs = self.it.get_input_details()
        for d in inputs:
            self.it.set_tensor(d["index"], np.zeros(d["shape"], dtype=d["dtype"]))
        self.inp, self.out = inputs[0], self.it.get_output_details()[0]
        self.slices = int(self.inp["shape"][1])
        self.quantized = self.inp["dtype"] == np.int8
        q = self.inp["quantization_parameters"]
        self.scale, self.zero = (float(q["scales"][0]), int(q["zero_points"][0])) if self.quantized else (1.0, 0)
        self.out_zero = int(self.out["quantization_parameters"]["zero_points"][0]) if self.quantized else 0

    def predict(self, samples: np.ndarray) -> np.ndarray:
        return self.infer(features(samples))

    def infer(self, spec: np.ndarray) -> np.ndarray:
        # Quantized like microwakeword.inference.Model (truncating cast), so scores match calibrate.py.
        x = (spec / self.scale + self.zero).astype(np.int8) if self.quantized else spec
        s, n = self.slices, len(spec) // self.slices
        out = np.empty(n, dtype=np.float32)
        for k in range(n):
            self.it.set_tensor(self.inp["index"], x[k * s:(k + 1) * s].reshape(self.inp["shape"]))
            self.it.invoke()
            out[k] = self.it.get_tensor(self.out["index"])[0][0]
        return (out - self.out_zero) / 255 if self.quantized else out

    def start(self) -> None:
        """Begin a new stream: fresh frontend, predictions counted from zero (the model state carries over)."""
        from pymicro_features import MicroFrontend

        self.frontend, self.pending, self.frames, self.count = MicroFrontend(), b"", np.zeros((0, 40), np.float32), 0

    def feed(self, samples: np.ndarray) -> np.ndarray:
        """Predictions for the next samples of the stream (stream indices self.count onwards)."""
        pcm, out, i = self.pending + pcm16(samples), [], 0
        while i + STEP * 2 <= len(pcm):
            r = self.frontend.process_samples(pcm[i:i + STEP * 2])
            i += r.samples_read * 2
            if r.features:
                out.append(r.features)
        self.pending = pcm[i:]
        frames = np.concatenate([self.frames, np.asarray(out, dtype=np.float32).reshape(-1, 40)])
        n = len(frames) // self.slices * self.slices
        self.frames = frames[n:]
        preds = self.infer(frames[:n])
        self.count += len(preds)
        return preds

    def accept_time(self, j: int) -> float:
        """End of the audio prediction j has seen, in seconds from the start of the stream."""
        return (STEP * (self.slices * j + self.slices - 1) + WINDOW) / SR


def normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def says_wake_word(text: str | None, phrases: list[str]) -> bool:
    t = f" {normalize(text or '')} "
    return any(f" {p} " in t for p in phrases if p)


def _init(model: str, window: int, cutoff: float, phrases: list[str]) -> None:
    global _detector, _cfg
    # The job's stop signal reaches the whole process group; only the parent handles it.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _detector = Detector(model)
    _cfg = {"window": window, "cutoff": cutoff, "phrases": [normalize(p) for p in phrases]}


def _scan(task: audio.Task) -> tuple[float, list[dict]]:
    """Triggers of one stream. Smoothing and cooldown run per block (a whole Arrow task, or 10 min of a file)."""
    w, cutoff = _cfg["window"], _cfg["cutoff"]
    seconds, found = 0.0, []
    _detector.start()
    for b in audio.blocks(task):
        j0, start = _detector.count, seconds  # stream index of the block's first prediction, block start in s
        seconds += len(b.audio) / SR
        p = _detector.feed(b.audio)
        if len(p) < w:
            continue
        ma = moving_average(p, w)
        last = -COOLDOWN
        for i in np.flatnonzero(ma > cutoff):
            if i < last + COOLDOWN:
                continue
            t_stream = _detector.accept_time(j0 + int(i) + w - 1)
            if t_stream < WARMUP_S:
                continue
            last = int(i)
            t = t_stream - start
            seg, offset = b.at(t)
            a, e = max(0, int((t - store.CONTEXT_BEFORE) * SR)), min(len(b.audio), int((t + store.CONTEXT_AFTER) * SR))
            found.append({
                "source": task.source, "ref": seg.ref, "offset": round(offset, 2),
                "score": round(float(ma[last:last + COOLDOWN].max()), 4),
                "peak": round(float(p[last:last + w + COOLDOWN].max()), 4),
                "text": seg.text, "match": says_wake_word(seg.text, _cfg["phrases"]), "pre": round(t - a / SR, 3),
                "audio": np.clip(b.audio[a:e] * 32767, -32768, 32767).astype(np.int16),
            })
    return seconds, found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--run", required=True, help="training run id below <project>/runs")
    ap.add_argument("--sources", nargs="+", required=True)
    ap.add_argument("--cutoff", type=float, help="trigger threshold (default: the run's calibrated cutoff)")
    ap.add_argument("--workers", type=int, default=max(1, min(12, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--no-auto", action="store_true", help="the audio may contain the wake word: label nothing automatically")
    ap.add_argument("--no-check", action="store_true", help="skip the automatic check after the scan")
    ap.add_argument("--job", type=Path, required=True)
    args = ap.parse_args()
    try:
        mine(args)
    except Exception as e:
        write_progress(args.job, state="failed", message=f"{type(e).__name__}: {e}")
        raise


def mine(args: argparse.Namespace) -> None:
    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    project = projects.load(args.project)
    run_dir = args.project / "runs" / args.run
    cal = json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("calibration") or {}
    models = sorted(run_dir.glob("*.tflite"))
    if not models or "probability_cutoff" not in cal:
        raise RuntimeError(f"run {args.run} has no calibrated model")
    cutoff, window = float(cal["probability_cutoff"]), int(cal["sliding_window_size"])
    mine_cutoff = args.cutoff if args.cutoff is not None else cutoff
    phrases = list(dict.fromkeys([project["wake_word"], *project.get("tts_phrases", [])]))

    progress = {"state": "running", "phase": "loading", "done_s": 0.0, "total_s": 0.0, "hits": 0, "rate": 0.0, "eta": None}
    write_progress(args.job, **progress, message="listing the audio")
    tasks = [t for s in args.sources for t in audio.tasks(s)]
    total = sum(t.seconds for t in tasks)
    if not tasks:
        raise RuntimeError("nothing to mine in the chosen sources")

    out = store.root(args.project) / f"{datetime.now():%Y%m%d-%H%M%S}"
    (out / "clips").mkdir(parents=True)
    meta = {"id": out.name, "run": args.run, "model": models[0].name, "cutoff": cutoff, "window": window,
            "mine_cutoff": mine_cutoff, "sources": args.sources, "hours_total": round(total / 3600, 2), "hours_done": 0.0,
            "overflow": 0, "auto": not args.no_auto, "state": "running", "started": datetime.now().isoformat(timespec="seconds"),
            "job": args.job.name}
    hits: list[dict] = []

    def save(state: str | None = None) -> None:
        store.write_json(out / "hits.json", hits)
        if state:
            meta["state"] = state
        store.write_json(out / "mine.json", meta)

    save()
    progress.update(mine=out.name, total_s=round(total), phase="mining")
    write_progress(args.job, **progress, message=f"starting {args.workers} workers")
    print(f"mining {len(tasks)} tasks, {total / 3600:.1f} h with run {args.run} (cutoff {mine_cutoff}, device {cutoff}, "
          f"window {window}), {args.workers} workers", flush=True)

    done, t0, last_write = 0.0, time.perf_counter(), 0.0
    with Pool(args.workers, initializer=_init, initargs=(str(models[0]), window, mine_cutoff, phrases)) as pool:
        for k, (seconds, found) in enumerate(pool.imap_unordered(_scan, tasks), 1):
            done += seconds
            for h in found:
                if len(hits) >= MAX_HITS:
                    meta["overflow"] += 1
                    continue
                clip = h.pop("audio")
                h["n"] = len(hits)
                h["seconds"] = round(len(clip) / SR, 2)
                sf.write(out / "clips" / f"{h['n']:05d}.wav", clip, SR, subtype="PCM_16")
                hits.append(h)
            now = time.perf_counter()
            if stop or k == len(tasks) or now - last_write > 1.0:
                last_write = now
                rate = done / (now - t0)
                meta["hours_done"] = round(done / 3600, 3)
                save()
                write_progress(args.job, **{**progress, "done_s": round(done), "hits": len(hits) + meta["overflow"],
                                            "rate": round(rate), "eta": round((total - done) / rate) if rate else None})
            if stop:
                pool.terminate()
                break
    state = "stopped" if stop else "done"
    elapsed = time.perf_counter() - t0
    meta.update(hours_done=round(done / 3600, 3), seconds=round(elapsed), finished=datetime.now().isoformat(timespec="seconds"))
    save(state)
    device = sum(1 for h in hits if h["score"] > cutoff)
    print(f"{state}: {done / 3600:.1f} h in {elapsed:.0f}s, {len(hits)} triggers ({device} at the device cutoff, "
          f"{device / max(done / 3600, 1e-9):.2f}/h), {sum(h['match'] for h in hits)} with the wake word in the transcript",
          flush=True)
    final = {**progress, "done_s": round(done), "hits": len(hits) + meta["overflow"],
             "rate": round(done / elapsed) if elapsed else 0, "eta": 0}
    message = None
    if state == "done" and hits and not args.no_check:
        write_progress(args.job, **{**final, "state": "running", "message": "starting the automatic check"})
        message = check(args, out.name)
        state = "stopped" if stop else state
    current = store.read_json(args.job / "progress.json", {})  # keeps what the check reported
    write_progress(args.job, **{**current, **final, "state": state, "phase": current.get("phase", "mining"),
                                **({"message": message} if message else {})})


def check(args: argparse.Namespace, mine_id: str) -> str | None:
    """Run the automatic check in the OmniVoice venv; returns a message when it could not finish."""
    exe = shutil.which("py-omni")
    if not exe:
        return "automatic check skipped: the py-omni launcher is missing"
    # Each launcher sets the CUDA libraries of its own venv; the TF venv's must not leak into the torch venv.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    cmd = [exe, "-m", "trainer.mining.triage", "--project", str(args.project), "--mine", mine_id, "--job", str(args.job), "--part"]
    code = subprocess.run(cmd, env=env).returncode  # the job's SIGTERM reaches the child through the process group
    return None if code == 0 else f"automatic check failed (exit {code}), see log; review the triggers by hand or run it again"


if __name__ == "__main__":
    main()
