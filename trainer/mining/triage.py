"""Automatic check of a mining scan's triggers (OmniVoice venv: torch on the GPU).

  py-omni -m trainer.mining.triage --project data/projects/x --mine <scan id> --job <job dir>

Per trigger:
  phones  the IPA phone recognizer (trainer/tts/phonetic.py) on the training cut; `contained` is the distance of its
          best matching stretch to the wake word
  text    the source transcript (Kartoffelphon) or, without one, Whisper on the 4.5 s context; `text_distance` is the
          closest sequence of one or two words to the wake word (espeak-ng phones; a sequence containing it counts)

Verdict: "review" when either signal says wake word (contained <= PHONES_MAX, text_distance <= TEXT_MAX, or the
transcript names it); otherwise "negative" if the scan's audio is declared free of the wake word (mine.json "auto",
true for Kartoffelphon whose transcripts are checked here anyway), else "review". Negative verdicts are labeled
right away (marked as automatic in auto.json); a person's label is never overridden.

Calibration (first Dscharwis scan, 234 triggers in Kartoffelphon, plus the 98 own recordings spliced into its speech):
the transcript rule flags 2 of 234 and catches spellings like "Javis" 0.11, "Charvis" 0.13, "Jarwis" 0.0 ("Garvis"
0.33 and "Marvin" 0.47 stay hard negatives); the phones flag 4 of 234. Neither proves that a clip is NOT the wake
word: the phones miss ~11 % of the spliced recordings and Whisper writes less than half of them (it hears the context
words or hallucinates), so a negative verdict also needs the source's guarantee.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import time
from pathlib import Path

from trainer import jobs, models
from trainer.mining import store
from trainer.tts import phonetic

PHONES_MAX = 0.30
TEXT_MAX = 0.30
BATCH = 32
TEXT_CHARS = 300


def update_progress(job: Path, **data) -> None:
    """Merge into progress.json: when the scan job runs the check, its own fields stay."""
    progress = store.read_json(job / "progress.json", {})
    progress.update(data, updated=time.time())
    jobs.write_progress_file(job, progress)


def normalize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def candidates(text: str) -> list[str]:
    words = normalize(text)
    return list(dict.fromkeys(" ".join(words[i:i + n]) for n in (1, 2) for i in range(len(words) - n + 1)))


def text_distances(texts: list[str], phrases: list[str], language: str) -> list[tuple[float, str]]:
    """Closest one- or two-word sequence of each text to any spelling (one espeak-ng call for all of them)."""
    targets = [list(t) for t in phonetic.phrases_phones(phrases, language)]
    per_text = [candidates(t) for t in texts]
    every = list(dict.fromkeys(c for cs in per_text for c in cs))
    phones = dict(zip(every, phonetic.phrases_phones(every, language))) if every else {}

    def dist(c: str) -> float:
        p = list(phones[c])
        return min(min(phonetic.distance(p, t), phonetic.contained_distance(p, t)) for t in targets)

    out = []
    for cs in per_text:
        out.append(min(((round(dist(c), 3), c) for c in cs), default=(1.0, "")))
    return out


def whisper(clips: list, language: str) -> list[str]:
    import torch
    from transformers import pipeline

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available; refusing to run Whisper on the CPU")
    asr = pipeline("automatic-speech-recognition", model=models.local(models.WHISPER), dtype=torch.float16, device="cuda")
    out = asr([{"raw": c, "sampling_rate": phonetic.RATE} for c in clips], batch_size=16,
              generate_kwargs={"language": language, "task": "transcribe"})
    return [o["text"].strip()[:TEXT_CHARS] for o in out]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--mine", required=True)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--part", action="store_true", help="runs inside the scan job: leave the job state to it")
    args = ap.parse_args()
    try:
        run(args)
    except Exception as e:
        update_progress(args.job, **({} if args.part else {"state": "failed"}), message=f"automatic check: {type(e).__name__}: {e}")
        raise


def run(args: argparse.Namespace) -> None:
    import soundfile as sf

    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    project = json.loads((args.project / "project.json").read_text(encoding="utf-8"))
    language = project["language"]
    phrases = list(dict.fromkeys([project["wake_word"], *(project.get("tts_phrases") or [])]))
    path = store.mine_dir(args.project, args.mine)
    meta = store.read_json(path / "mine.json", {})
    hits = store.hits(path)
    base = {} if args.part else {"state": "running"}
    update_progress(args.job, **base, phase="triage", mine=args.mine, triage={"done": 0, "total": len(hits)},
                    message="loading the phone recognizer")
    t0 = time.perf_counter()
    checker = phonetic.PhoneChecker(language, phrases)

    result: dict[str, dict] = {}
    for i in range(0, len(hits), BATCH):
        if stop:
            break
        batch = hits[i:i + BATCH]
        for h, s in zip(batch, checker.score([store.cut(path, h) for h in batch])):
            result[str(h["n"])] = {"phones": " ".join(s.phones), "contained": s.contained}
        update_progress(args.job, triage={"done": len(result), "total": len(hits)}, message="checking the phones")

    done = [h for h in hits if str(h["n"]) in result]
    untranscribed = [h for h in done if not h.get("text")]
    if untranscribed and not stop:
        update_progress(args.job, message=f"transcribing {len(untranscribed)} triggers without a transcript (Whisper)")
        texts = whisper([sf.read(path / "clips" / f"{h['n']:05d}.wav", dtype="float32")[0] for h in untranscribed], language)
        for h, t in zip(untranscribed, texts):
            result[str(h["n"])]["asr"] = t
    texts = [h.get("text") or result[str(h["n"])].get("asr") or "" for h in done]
    seen = labeled_elsewhere(args.project, args.mine)
    for h, (d, words) in zip(done, text_distances(texts, phrases, language)):
        r = result[str(h["n"])]
        has_text = bool(h.get("text") or r.get("asr"))
        r.update(text_distance=d, text_words=words)
        other = next((seen[k] for k in position_keys(h) if k in seen), None)
        if other:
            r.update(verdict="duplicate", reason=f"already labeled in scan {other}")
            continue
        reasons = []
        if h.get("match"):
            reasons.append("transcript names the wake word")
        elif has_text and d <= TEXT_MAX:
            reasons.append(f"text “{words}” sounds like it ({d:.2f})")
        if r["contained"] <= PHONES_MAX:
            reasons.append(f"phones sound like it ({r['contained']:.2f})")
        if not reasons and not meta.get("auto", True):
            reasons.append("the audio may contain the wake word")
        r["verdict"] = "review" if reasons else "negative"
        r["reason"] = "; ".join(reasons)

    with store.locked(path):
        old = store.triage(path)
        store.write_json(path / "triage.json", {**old, **result})
    negative = [int(k) for k, r in result.items() if r["verdict"] == "negative"]
    review = [int(k) for k, r in result.items() if r["verdict"] == "review"]
    duplicate = [int(k) for k, r in result.items() if r["verdict"] == "duplicate"]
    if negative:
        store.set_labels(args.project, args.mine, negative, "negative", by_triage=True)
    if duplicate:  # the same stretch of audio must not land in training twice
        store.set_labels(args.project, args.mine, duplicate, "skip", by_triage=True)
    stale = [n for n in review if str(n) in store.auto(path)]  # automatic labels a rerun no longer supports
    if stale:
        store.set_labels(args.project, args.mine, stale, None, by_triage=True)
    summary = (f"checked {len(result)}/{len(hits)} triggers in {time.perf_counter() - t0:.0f}s: {len(negative)} labeled "
               f"as false triggers, {len(review)} to review, {len(duplicate)} already labeled in another scan, "
               f"{len(untranscribed)} transcribed by Whisper")
    print(summary, flush=True)
    end = {} if args.part else {"state": "stopped" if stop else "done", "eta": 0}
    update_progress(args.job, **end, triage={"done": len(result), "total": len(hits), "negative": len(negative),
                                             "review": len(review), "duplicate": len(duplicate)}, message=summary)


def position_keys(hit: dict) -> list[tuple[str, int]]:
    """A trigger's place in its source, tolerant to a second of jitter between models."""
    t = round(hit["offset"])
    return [(hit["ref"], t + d) for d in (0, -1, 1)]


def labeled_elsewhere(project: Path, mine_id: str) -> dict[tuple[str, int], str]:
    """Positions of the triggers other scans put into training (negative or positive)."""
    seen = {}
    for other in store.mine_dirs(project):
        if other.name == mine_id:
            continue
        ls = store.labels(other)
        for h in store.hits(other):
            if ls.get(str(h["n"])) in ("negative", "positive"):
                seen[(h["ref"], round(h["offset"]))] = other.name
    return seen


if __name__ == "__main__":
    main()
