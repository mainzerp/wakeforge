"""One training run of a project as a background job (runs in the TF venv).

  py-train -m trainer.train.run --project data/projects/x --profile fast --job <job dir>

Phases, all reported in <job dir>/progress.json:

  featurize   augment + spectrograms for every source item not yet cached (trainer/train/featurize.py)
  train       microWakeWord's model_train_eval on the GPU (subprocess, its log parsed for steps and metrics),
              then the streaming int8 TFLite export of the best checkpoint
  export      a second model_train_eval without training exports the last checkpoint
  calibrate   operating point by false accepts per hour for both checkpoints (trainer/train/calibrate.py);
              the better one becomes the run's model
  package     <run>/<slug>.tflite + <slug>.json (ESPHome manifest) + run.json (params, sources, metrics)

Runs live in <project>/runs/<YYYYMMDD-HHMMSS>-<profile>/. SIGTERM stops the run (no resume).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from trainer import jobs, projects
from trainer.train import calibrate, featurize
from trainer.train.sources import Source, project_sources

PROFILES = {
    "fast": {"steps": 10000, "eval_every": 500, "label": "Fast"},
    "thorough": {"steps": 40000, "eval_every": 1000, "label": "Thorough"},
}
BATCH_SIZE = 32
# Learning rate phases as (share of the steps, rate). A constant 0.001 made the validation metrics jump from one
# evaluation to the next (AVR 0.07-0.93 late in a 40k run), so best_weights was a lucky pick; the lower rates at
# the end let the model settle (see CHECKPOINTS).
LR_SCHEDULE = [(0.6, 0.001), (0.25, 0.0003), (0.15, 0.0001)]


def lr_phases(steps: int) -> tuple[list[int], list[float]]:
    """Split the step count over LR_SCHEDULE; the last phase takes the rounding remainder."""
    counts = [round(steps * share) for share, _ in LR_SCHEDULE[:-1]]
    counts.append(steps - sum(counts))
    return counts, [rate for _, rate in LR_SCHEDULE]
MODEL_FLAGS = [
    "mixednet",
    "--pointwise_filters", "128,128,128,128",
    "--repeat_in_block", "1,1,1,1",
    "--mixconv_kernel_sizes", "[5], [7,11], [9,15], [23]",
    "--residual_connection", "0,0,0,0",
    "--first_conv_filters", "64",
    "--first_conv_kernel_size", "5",
    "--stride", "2",
]
STRIDE = 2
CLIP_MS = 1500
QUANT_DIR, QUANT_FILE = "tflite_stream_state_internal_quant", "stream_state_internal_quant.tflite"
# microWakeWord keeps the weights with the best validation AVR, which can be a spike early in the 0.001 phase
# (step 15000 of 40000 in a measured run), and the last weights, settled at the lowest rate. Both are exported and
# calibrated; ties go to the first entry.
CHECKPOINTS = ("last", "best")
# Manifest keys ESPHome accepts (micro_wake_word, manifest version 2)
TENSOR_ARENA = 30000
MIN_ESPHOME = "2024.7.0"

STEP_RE = re.compile(r"Validation Batch #(\d+):.*?Mini-Batch #(\d+)")
VAL_RE = re.compile(
    r"Step (\d+) \(nonstreaming\): Validation: recall at no faph = ([\d.]+) with cutoff ([\d.]+), accuracy = ([\d.]+)%, "
    r"recall = ([\d.]+)%, precision = ([\d.]+)%, ambient false positives = (\d+), estimated false positives per hour = "
    r"([\d.]+), loss = ([\d.]+), auc = ([\d.]+), average viable recall = ([\d.]+)")
BEST_RE = re.compile(r"best maximization quantity of ([\d.]+)%")
NOISE = ("Validation Batch", "TensorSpec(", "tf_tfl_flatbuffer_helpers", "flatbuffer_export", "XNNPACK")


def spectrogram_length() -> int:
    """model_train_eval.load_config for MODEL_FLAGS: final layer frames + slices dropped by valid padding."""
    final = 1 + int((16000 * CLIP_MS / 1000 - 480) / (STRIDE * 160))
    dropped = 4 + sum(max(k) - 1 for k in ([5], [7, 11], [9, 15], [23])) * STRIDE
    return final + dropped


def pick_checkpoint(results: dict[str, dict]) -> str:
    """The checkpoint meeting the false accept target with the higher recall; if none meets it, fewer false accepts."""
    def key(name: str) -> tuple:
        c = results[name]
        return (not c["met_target"], -c["recall"] if c["met_target"] else c["false_accepts_per_hour"], -c["recall"])
    return min(results, key=key)


def checkpoint_summary(name: str, step: int | None, cal: dict) -> dict:
    return {"name": name, "step": step, **{k: cal[k] for k in (
        "probability_cutoff", "sliding_window_size", "recall", "false_accepts_per_hour", "met_target")}}


class Run:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.project_dir = Path(args.project)
        self.project = projects.load(self.project_dir)
        self.profile = {**PROFILES[args.profile], "steps": args.steps or PROFILES[args.profile]["steps"]}
        self.job: Path = args.job
        self.dir = self.project_dir / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{args.profile}"
        self.dir.mkdir(parents=True)
        self.stop = False
        self.child: subprocess.Popen | None = None
        self.best: tuple[str | None, int | None] = (None, None)  # (validation AVR, step) of best_weights
        self.progress = {"state": "running", "phase": "featurize", "run": self.dir.name, "profile": args.profile,
                         "steps": self.profile["steps"], "step": 0, "history": []}
        self.t_phase = time.perf_counter()
        self.meta = {"id": self.dir.name, "profile": args.profile, "steps": self.profile["steps"],
                     "target_faph": args.target_faph, "started": datetime.now().isoformat(timespec="seconds"),
                     "job": self.job.name, "state": "running", "wake_word": self.project["wake_word"]}
        signal.signal(signal.SIGTERM, self.on_term)
        signal.signal(signal.SIGINT, self.on_term)

    def on_term(self, *_):
        self.stop = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def write(self, **update) -> None:
        self.progress.update(update, updated=time.time())
        jobs.write_progress_file(self.job, self.progress)

    def save_meta(self, **update) -> None:
        self.meta.update(update)
        (self.dir / "run.json").write_text(json.dumps(self.meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def phase(self, name: str, message: str) -> None:
        print(f"== {name}: {message}", flush=True)
        self.t_phase = time.perf_counter()
        self.write(phase=name, message=message)

    # -- featurize --------------------------------------------------------------------------------------------
    def featurize(self, sources: list[Source]) -> None:
        items = [i for s in sources for i in s.items]
        self.phase("featurize", "augmenting clips and computing spectrograms")

        def report(s: dict) -> None:
            el = time.perf_counter() - s["t0"]
            rate = s["done"] / el if el > 0 else 0
            self.write(featurize={"done": s["done"], "total": s["total"], "rate": round(rate, 1),
                                  "eta": round((s["total"] - s["done"]) / rate) if rate else None})

        featurize.featurize(items, self.args.workers, report, lambda: self.stop)
        cached = len(items) - sum(1 for i in items if not (i.cache / "done").is_file())
        print(f"features ready: {cached}/{len(items)} items in {time.perf_counter() - self.t_phase:.0f}s", flush=True)

    # -- train ------------------------------------------------------------------------------------------------
    def config(self, sources: list[Source]) -> Path:
        features = []
        for s in sources:
            for d in s.dirs:
                features.append(self.feature(d, s, s.sampling_weight))
            train_counts = [featurize.split_counts(i).get("training", 0) for i in s.items]
            total = sum(train_counts)
            for item, n in zip(s.items, train_counts):
                # One provider per cached item; weights proportional to size so the source as a whole keeps its
                # sampling weight (microWakeWord first picks a provider by weight, then a random spectrogram).
                features.append(self.feature(str(item.cache), s, s.sampling_weight * n / total if total else 0.0))
        steps, rates = lr_phases(self.profile["steps"])
        self.save_meta(batch_size=BATCH_SIZE, training_steps=steps, learning_rates=rates)
        config = {
            "batch_size": BATCH_SIZE,
            "clip_duration_ms": CLIP_MS,
            "eval_step_interval": self.profile["eval_every"],
            "window_step_ms": 10,
            "train_dir": str(self.dir / "mww"),
            "learning_rates": rates,
            "training_steps": steps,
            "positive_class_weight": [1],
            "negative_class_weight": [20],
            "maximization_metric": "average_viable_recall",
            "minimization_metric": None,
            "target_minimization": 0.9,
            "time_mask_count": [0],
            "time_mask_max_size": [0],
            "freq_mask_count": [0],
            "freq_mask_max_size": [0],
            "features": features,
        }
        path = self.dir / "training_parameters.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    @staticmethod
    def feature(path: str, s: Source, weight: float) -> dict:
        return {"features_dir": path, "sampling_weight": round(weight, 6), "penalty_weight": s.penalty_weight,
                "truth": s.truth, "truncation_strategy": s.truncation, "type": "mmap"}

    def train(self, config: Path) -> None:
        """Train, then export both checkpoints as streaming TFLite models; calibration picks between them."""
        self.phase("train", "loading the feature sets")
        self.mww(config, "--train", "1", "--restore_checkpoint", "1", "--use_weights", "best_weights")
        if self.stop:
            return
        self.keep("best")
        self.phase("export", "exporting the last checkpoint")
        self.mww(config, "--train", "0", "--use_weights", "last_weights")
        if self.stop:
            return
        self.keep("last")

    def checkpoint(self, name: str) -> Path:
        return self.dir / "mww" / f"tflite_{name}" / QUANT_FILE

    def keep(self, name: str) -> None:
        """Move microWakeWord's TFLite output aside so the next export does not overwrite it."""
        src = self.dir / "mww" / QUANT_DIR
        if not (src / QUANT_FILE).is_file():
            raise RuntimeError(f"no streaming TFLite model for the {name} checkpoint")
        dst = self.checkpoint(name).parent
        shutil.rmtree(dst, ignore_errors=True)
        src.rename(dst)

    def mww(self, config: Path, *args: str) -> None:
        """Run model_train_eval, parsing its log for steps, validation metrics and the best checkpoint's step."""
        cmd = [sys.executable, "-u", "-m", "trainer.train.mww_entry", "--training_config", str(config),
               "--test_tf_nonstreaming", "0", "--test_tflite_nonstreaming", "0",
               "--test_tflite_nonstreaming_quantized", "0", "--test_tflite_streaming", "0",
               "--test_tflite_streaming_quantized", "1", *args, *MODEL_FLAGS]
        self.child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        steps, every = self.profile["steps"], self.profile["eval_every"]
        t_train, last_write, buf = None, 0.0, b""
        while True:
            chunk = self.child.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if m := STEP_RE.search(line):
                    step = (int(m[1]) - 1) * every + int(m[2])
                    if t_train is None:
                        t_train = (time.perf_counter(), step)
                        self.write(message="training")
                    if time.perf_counter() - last_write > 1:
                        el = time.perf_counter() - t_train[0]
                        rate = (step - t_train[1]) / el if el > 0 else 0
                        self.write(step=step, rate=round(rate, 1),
                                   eta=round((steps - step) / rate) if rate else None)
                        last_write = time.perf_counter()
                    continue
                if m := VAL_RE.search(line):
                    v = [float(x) for x in m.groups()]
                    self.progress["history"].append({
                        "step": int(v[0]), "recall_no_faph": v[1] / 100, "accuracy": v[3] / 100,
                        "recall": v[4] / 100, "precision": v[5] / 100, "faph": v[7], "auc": v[9], "avr": v[10]})
                    self.write(step=int(v[0]))
                elif (m := BEST_RE.search(line)) and m[1] != self.best[0] and self.progress["history"]:
                    # best_weights is saved whenever this quantity rises, right after the validation above
                    self.best = (m[1], self.progress["history"][-1]["step"])
                elif "Converting" in line or "Saving streaming" in line:
                    self.write(phase="export", message=line.split(":")[-1].strip())
                if not any(n in line for n in NOISE):
                    print(line.replace("INFO:absl:", ""), flush=True)
        code = self.child.wait()
        if self.stop:
            return
        if code:
            raise RuntimeError(f"model_train_eval exited with {code}, see log")

    # -- calibrate + package ----------------------------------------------------------------------------------
    def calibrate(self, sources: list[Source]) -> tuple[str, dict]:
        """Calibrate both checkpoints and record them in run.json; returns the chosen one and its calibration."""
        self.phase("calibrate", "streaming both checkpoints over held-out clips and ambient audio")
        steps = {"last": self.profile["steps"], "best": self.best[1]}
        results = {}
        for name in CHECKPOINTS:
            print(f"-- {name} checkpoint (step {steps[name]})", flush=True)
            results[name] = calibrate.calibrate(self.checkpoint(name), sources, self.args.target_faph, stride=STRIDE,
                                                log=lambda m: print(m, flush=True))
        chosen = pick_checkpoint(results)
        print(f"chosen: {chosen} checkpoint", flush=True)
        self.save_meta(checkpoint=chosen, checkpoints=[checkpoint_summary(n, steps[n], results[n]) for n in CHECKPOINTS])
        return chosen, results[chosen]

    def package(self, chosen: str, cal: dict) -> dict:
        slug = re.sub(r"[^a-z0-9]+", "_", self.project["wake_word"].lower()).strip("_") or self.project_dir.name
        tflite = self.dir / f"{slug}.tflite"
        shutil.copy2(self.checkpoint(chosen), tflite)
        manifest = {
            "type": "micro",
            "wake_word": self.project["wake_word"],
            "author": self.project.get("author") or "",
            "website": self.project.get("website") or "",
            "model": tflite.name,
            "trained_languages": [self.project["language"]],
            "version": 2,
            "micro": {
                "probability_cutoff": cal["probability_cutoff"],
                "sliding_window_size": cal["sliding_window_size"],
                "feature_step_size": 10,
                "tensor_arena_size": TENSOR_ARENA,
                "minimum_esphome_version": MIN_ESPHOME,
            },
        }
        (self.dir / f"{slug}.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"model": tflite.name, "manifest": f"{slug}.json", "size": tflite.stat().st_size}

    def run(self) -> None:
        assert spectrogram_length() <= featurize.CROP, "stored spectrograms are shorter than the model input"
        sources = project_sources(self.project_dir)
        self.save_meta(sources=[s.summary() for s in sources])
        t0 = time.perf_counter()
        self.featurize(sources)
        if self.stop:
            return self.finish("stopped")
        self.train(self.config(sources))
        if self.stop:
            return self.finish("stopped")
        chosen, cal = self.calibrate(sources)
        files = self.package(chosen, cal)
        self.save_meta(calibration=cal, files=files, history=self.progress["history"],
                       seconds=round(time.perf_counter() - t0))
        self.finish("done")

    def finish(self, state: str) -> None:
        self.save_meta(state=state, finished=datetime.now().isoformat(timespec="seconds"),
                       history=self.progress["history"])
        self.write(state=state, phase="done" if state == "done" else self.progress["phase"], eta=0,
                   message=f"run {self.dir.name} {state}")
        print(f"== {state}: {self.dir.name}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--profile", choices=list(PROFILES), default="fast")
    ap.add_argument("--steps", type=int, default=0, help="override the profile's training steps")
    ap.add_argument("--target-faph", type=float, default=0.25)
    ap.add_argument("--workers", type=int, default=featurize.default_workers())
    args = ap.parse_args()
    run = Run(args)
    try:
        run.run()
    except Exception as e:
        run.write(state="failed", message=f"{type(e).__name__}: {e}")
        run.save_meta(state="failed", error=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
