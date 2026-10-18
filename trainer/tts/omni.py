"""Synthetic wake word positives with OmniVoice voice cloning (runs in the OmniVoice venv).

  py-omni -m trainer.tts.omni run --project data/projects/x --job <job dir> --count 20000
  py-omni -m trainer.tts.omni run --mode confusable --project data/projects/x --job <job dir> --count 5000
  py-omni -m trainer.tts.omni preview --language de --phrases Jarvis Dscharwis --out <dir>

Every clip clones a random reference voice from the bank (data/voices/<language>/) or, for --design-share of the
batches, a voice designed from speaker attributes. Tempo and punctuation vary per clip. A phone recognizer scores
each clip against the espeak pronunciation of the spellings (trainer.tts.phonetic): close clips go to the corpus,
clearly different ones (TTS slips) to the negative candidates, the gray zone in between is dropped.

With --mode confusable, `run` speaks the project's sound-alike words instead (trainer.tts.confusables) into
generated/tts-confusable/, keeping only clips that do not sound like (or contain) the wake word.

`run` is resumable: it tops the corpus up to --count clips. SIGTERM stops after the current batch.
Progress goes to <job dir>/progress.json.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly

from trainer import jobs, models
from trainer.tts import corpus
from trainer.tts.phonetic import MIN_NEGATIVE_PHONES, POSITIVE_MAX, PhoneChecker, Score
from trainer.tts.voices import load_bank, voice_dir

ENGINE = "omnivoice"
SHARD_SIZE = 1024
PUNCTUATION = [(".", 0.5), ("!", 0.25), ("?", 0.15), ("", 0.1)]
DESIGN_ATTRS = {
    "gender": ["male", "female"],
    "age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
    "pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
}


def write_progress(job: Path, **data) -> None:
    data["updated"] = time.time()
    jobs.write_progress_file(job, data)


class Cycle:
    """Draw items in shuffled rounds so every one is used equally often."""

    def __init__(self, items: list, rng: random.Random):
        self.items, self.rng, self.pool = items, rng, []

    def take(self, k: int) -> list:
        while len(self.pool) < k:
            batch = list(range(len(self.items)))
            self.rng.shuffle(batch)
            self.pool.extend(batch)
        out, self.pool = self.pool[:k], self.pool[k:]
        return out


class Synth:
    def __init__(self, language: str, steps: int, phrases: list[str] | None):
        from omnivoice import OmniVoice, OmniVoiceGenerationConfig

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA not available; refusing to run OmniVoice on the CPU")
        self.language = language
        self.model = OmniVoice.from_pretrained(models.local(models.OMNIVOICE), device_map="cuda:0", dtype=torch.float16)
        self.config = OmniVoiceGenerationConfig(num_step=steps)
        self.rate = self.model.sampling_rate
        g = np.gcd(corpus.SAMPLE_RATE, self.rate)
        self.up, self.down = corpus.SAMPLE_RATE // g, self.rate // g
        self.bank = load_bank(language)
        self.prompts = [self.model.create_voice_clone_prompt(ref_audio=str(voice_dir(language) / f"{v['id']}.wav"),
                                                             ref_text=v["text"]) for v in self.bank]
        self.checker = PhoneChecker(language, phrases) if phrases else None

    def generate(self, texts: list[str], speeds: list[float], prompts=None, instructs=None) -> list[np.ndarray]:
        try:
            wavs = self._generate(texts, speeds, prompts, instructs)
        except ValueError as e:
            # A clip that comes out as pure silence is empty after OmniVoice's silence removal, and the peak
            # normalization of voice-design clips then fails the whole batch. Redo it clip by clip; an empty clip
            # is dropped by the caller as too short.
            if "zero-size array" not in str(e):
                raise
            wavs = []
            for i in range(len(texts)):
                try:
                    wavs += self._generate(texts[i:i + 1], speeds[i:i + 1], prompts and prompts[i:i + 1],
                                           instructs and instructs[i:i + 1])
                except ValueError as e:
                    if "zero-size array" not in str(e):
                        raise
                    wavs.append(np.zeros(0, dtype=np.float32))
        out = []
        for w in wavs:
            w = np.asarray(w, dtype=np.float32).squeeze()
            out.append(corpus.trim(resample_poly(w, self.up, self.down).astype(np.float32)) if w.size else w.reshape(0))
        return out

    def _generate(self, texts: list[str], speeds: list[float], prompts, instructs) -> list:
        return self.model.generate(text=texts, language=self.language, speed=speeds, voice_clone_prompt=prompts,
                                   instruct=instructs, generation_config=self.config)

    def score(self, clips: list[np.ndarray]) -> list[Score]:
        if self.checker is None:
            return [Score([], 0.0, "positive") for _ in clips]
        return self.checker.score(clips)


def design_voices() -> list[str]:
    return [f"{g}, {a}, {p}" for g in DESIGN_ATTRS["gender"] for a in DESIGN_ATTRS["age"] for p in DESIGN_ATTRS["pitch"]
            if not (a == "child" and p in ("very low pitch", "low pitch"))]


def with_punctuation(phrase: str, rng: random.Random) -> str:
    marks, weights = zip(*PUNCTUATION)
    return phrase.rstrip(".!?") + rng.choices(marks, weights)[0]


def load_project(path: Path) -> dict:
    project = json.loads((path / "project.json").read_text(encoding="utf-8"))
    project["phrases"] = project.get("tts_phrases") or [project["wake_word"]]
    return project


def run(args: argparse.Namespace) -> None:
    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    project_dir = Path(args.project)
    project = load_project(project_dir)
    wake = project["phrases"]
    confusable = args.mode == "confusable"
    # positive mode speaks the spellings: close -> corpus, clearly different -> negative candidates.
    # confusable mode speaks the sound-alikes: everything that does not sound like the wake word is kept.
    phrases = (project.get("confusables") or []) if confusable else wake
    if not phrases:
        raise RuntimeError("no sound-alike words in this project; add some in the wake word step")
    out = corpus.confusable_dir(project_dir) if confusable else corpus.corpus_dir(project_dir)
    index = corpus.load_index(out)
    if index["voice"] not in (None, ENGINE):
        raise RuntimeError(f"corpus was generated with {index['voice']}; reset it before using {ENGINE}")
    start = index["total"]
    neg_out = corpus.negative_dir(project_dir)
    neg_index = corpus.load_index(neg_out)
    negatives_start = neg_index["total"]
    progress = {"state": "running", "phase": "loading", "done": start, "start": start, "total": args.count, "rejected": 0,
                "filtered": 0, "negatives": negatives_start, "rate": 0.0}
    write_progress(args.job, **progress, message="loading OmniVoice and the phonetic check")

    no_filter = args.no_filter and not confusable
    synth = Synth(project["language"], args.steps, None if no_filter else wake)
    designs = design_voices()
    names = [f"bank:{v['id']}" for v in synth.bank] + [f"design:{d}" for d in designs]
    for idx in (index, neg_index):
        idx["voice"] = ENGINE
        idx.setdefault("voices", [])
        idx["phrases"] += [p for p in phrases if p not in idx["phrases"]]
        idx["voices"] += [n for n in names if n not in idx["voices"]]
    voice_pos = {n: i for i, n in enumerate(index["voices"])}
    neg_voice_pos = {n: i for i, n in enumerate(neg_index["voices"])}
    print(f"OmniVoice ready ({args.mode}): {len(synth.bank)} bank voices, {len(designs)} designs, "
          f"{len(phrases)} phrases, steps {args.steps}, filter {'off' if no_filter else 'on'}", flush=True)
    if not synth.bank and args.design_share < 1:
        print("no voice bank for this language, using voice design only", flush=True)

    rng = random.Random(f"{time.time()}-{start}")
    bank_cycle, design_cycle = Cycle(synth.bank, rng), Cycle(designs, rng)
    new_buf = lambda: {"clips": [], "speaker": [], "phrase": [], "scales": []}  # noqa: E731
    buf, neg_buf = new_buf(), new_buf()
    done, rejected, filtered, batch_no, t0 = start, 0, 0, 0, time.perf_counter()
    timing = {"tts": 0.0, "check": 0.0, "write": 0.0}
    progress["phase"] = "generating"
    write_progress(args.job, **progress, eta=None)

    def flush(force: bool = False) -> None:
        for path, idx, b in ((out, index, buf), (neg_out, neg_index, neg_buf)):
            if b["clips"] and (force or len(b["clips"]) >= SHARD_SIZE):
                t = time.perf_counter()
                corpus.append_shard(path, idx, b["clips"], b["speaker"], b["phrase"], b["scales"])
                timing["write"] += time.perf_counter() - t
                for v in b.values():
                    v.clear()

    def collect(batch: list[tuple]) -> None:
        nonlocal done, filtered
        t = time.perf_counter()
        scores = synth.score([b[0] for b in batch])
        timing["check"] += time.perf_counter() - t
        for (clip, voice, phrase, speed, design), s in zip(batch, scores):
            if confusable:
                # A sound-alike that came out like the wake word (or holds it) would contradict the positives,
                # one with hardly any phones is hum from a failed generation.
                if s.band == "positive" or s.contained <= POSITIVE_MAX or len(s.phones) < MIN_NEGATIVE_PHONES:
                    filtered += 1
                    continue
                positive = done < args.count
                if not positive:
                    continue
            else:
                if s.band == "gray" or (s.band == "positive" and done >= args.count):
                    filtered += s.band == "gray"
                    continue
                positive = s.band == "positive"
            b, pos, idx = (buf, voice_pos, index) if positive else (neg_buf, neg_voice_pos, neg_index)
            b["clips"].append(clip / np.abs(clip).max() * 0.9)
            b["speaker"].append(pos[voice])
            b["phrase"].append(idx["phrases"].index(phrase))
            b["scales"].append([speed, 1.0 if design else 0.0, float(args.steps), s.distance])
            done += positive
        flush()
        rate = (done - start) / (time.perf_counter() - t0)
        write_progress(args.job, **{**progress, "done": done, "rejected": rejected, "filtered": filtered,
                                    "negatives": neg_index["total"] + len(neg_buf["clips"]), "rate": round(rate, 2),
                                    "eta": round((args.count - done) / rate) if rate else None})

    while not stop and done < args.count:
        batch_no += 1
        design = not synth.bank or rng.random() < args.design_share
        n = args.batch
        chosen = [phrases[rng.randrange(len(phrases))] for _ in range(n)]
        texts = [with_punctuation(p, rng) for p in chosen]
        speeds = [round(rng.uniform(*args.speed), 3) for _ in range(n)]
        if design:
            picks = design_cycle.take(n)
            voice_names = [f"design:{designs[i]}" for i in picks]
            kw = {"instructs": [designs[i] for i in picks]}
        else:
            picks = bank_cycle.take(n)
            voice_names = [f"bank:{synth.bank[i]['id']}" for i in picks]
            kw = {"prompts": [synth.prompts[i] for i in picks]}
        t = time.perf_counter()
        clips = synth.generate(texts, speeds, **kw)
        timing["tts"] += time.perf_counter() - t
        batch = [(c, voice_names[i], chosen[i], speeds[i], design) for i, c in enumerate(clips)
                 if args.min_seconds <= len(c) / corpus.SAMPLE_RATE <= args.max_seconds]
        rejected += n - len(batch)
        collect(batch)
        if batch_no % 20 == 0:
            print(f"{done}/{args.count} {(done - start) / (time.perf_counter() - t0):.1f} clips/s, "
                  f"rejected {rejected}, filtered {filtered}", flush=True)
    flush(force=True)
    state = "stopped" if stop and done < args.count else "done"
    elapsed = time.perf_counter() - t0
    write_progress(args.job, **{**progress, "state": state, "done": done, "rejected": rejected, "filtered": filtered,
                                "negatives": neg_index["total"], "rate": round((done - start) / elapsed, 2), "eta": 0})
    dropped = (f"{filtered} sounded like the wake word" if confusable else
               f"{filtered} gray zone, {neg_index['total'] - negatives_start} negative candidates")
    print(f"{state}: {done} clips ({done - start} new, {rejected} rejected by length, {dropped}) in {elapsed:.0f}s, "
          + ", ".join(f"{k} {v:.1f}s" for k, v in timing.items()), flush=True)


def preview(args: argparse.Namespace) -> None:
    """--n clips per phrase in one batch, alternating male/female bank voices (designed voices without a bank)."""
    synth = Synth(args.language, args.steps, args.phrases)
    rng = random.Random()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    by_gender = {g: [i for i, v in enumerate(synth.bank) if v.get("gender") == g] for g in ("m", "f")}
    designs = design_voices()
    texts, speeds, prompts, instructs, labels = [], [], [], [], []
    for phrase in args.phrases:
        for k in range(args.n):
            pool = by_gender["m" if k % 2 == 0 else "f"] or list(range(len(synth.bank)))
            texts.append(phrase)
            speeds.append(round(rng.uniform(*args.speed), 2))
            if pool:
                i = rng.choice(pool)
                prompts.append(synth.prompts[i])
                labels.append((phrase, synth.bank[i]["id"]))
            else:
                d = rng.choice(designs)
                instructs.append(d)
                labels.append((phrase, d))
    clips = synth.generate(texts, speeds, prompts=prompts or None, instructs=instructs or None)
    scores = synth.score(clips)
    result = []
    for i, ((phrase, voice), speed, clip, s) in enumerate(zip(labels, speeds, clips, scores)):
        if not clip.size:
            continue
        sf.write(out / f"{i}.wav", clip / np.abs(clip).max() * 0.9, corpus.SAMPLE_RATE, subtype="PCM_16")
        result.append({"file": f"{i}.wav", "phrase": phrase, "voice": voice, "speed": speed,
                       "seconds": round(len(clip) / corpus.SAMPLE_RATE, 2), "transcript": " ".join(s.phones),
                       "distance": s.distance, "band": s.band, "hit": s.band == "positive"})
    targets = {p: " ".join(t) for p, t in synth.checker.targets.items()}
    print(json.dumps({"engine": ENGINE, "targets": targets, "clips": result}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "preview"):
        p = sub.add_parser(name)
        p.add_argument("--steps", type=int, default=16)
        p.add_argument("--speed", type=float, nargs=2, default=[0.85, 1.2])
    r = sub.choices["run"]
    r.add_argument("--project", required=True)
    r.add_argument("--job", type=Path, required=True)
    r.add_argument("--count", type=int, required=True)
    r.add_argument("--batch", type=int, default=32)
    r.add_argument("--design-share", type=float, default=0.15)
    r.add_argument("--no-filter", action="store_true", help="positive mode only; sound-alikes are always checked")
    r.add_argument("--mode", choices=["positive", "confusable"], default="positive")
    r.add_argument("--min-seconds", type=float, default=0.3)
    r.add_argument("--max-seconds", type=float, default=2.0)
    pv = sub.choices["preview"]
    pv.add_argument("--language", required=True)
    pv.add_argument("--phrases", nargs="+", required=True)
    pv.add_argument("--out", required=True)
    pv.add_argument("--n", type=int, default=2, help="clips per phrase")
    args = parser.parse_args()
    if args.command == "preview":
        preview(args)
        return
    try:
        run(args)
    except Exception as e:
        write_progress(args.job, state="failed", message=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
