"""What to do next in a project: one recommendation plus per-step notes, derived from the files alone.

The workflow is a loop, not a line: generate -> train -> mine (scan, check, review) -> train again -> mine again ...
The guide tells which part of the loop is due. Most important: a model is "stale" when the training data changed
since it was trained (new mined clips, recordings, synthetic samples), compared per source with the clip counts the
latest finished run recorded in its run.json.

Pure Python (no TF, no torch): the server calls it on every poll of the project page.
"""

from __future__ import annotations

import json
from pathlib import Path

from trainer import export, jobs, recipe, setup
from trainer.mining import store as mining
from trainer.train.sources import project_sources
from trainer.tts import confusables, corpus

KIND_STEP = {"generate": "generate", "confusables": "generate", "train": "train", "mining": "mining", "triage": "mining"}
KIND_LABEL = {"generate": "Synthetic samples are being generated", "confusables": "Sound-alikes are being generated",
              "train": "A model is being trained", "mining": "Real speech is being scanned for false triggers",
              "triage": "The triggers are being checked"}


def latest_run(project: Path) -> dict | None:
    root = project / "runs"
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if run.get("state") == "done" and run.get("calibration") and any(d.glob("*.tflite")):
            return run
    return None


def changes(sources: list, run: dict) -> list[dict]:
    """Project sources whose clip count differs from what the run trained on (shared datasets never change)."""
    before = {s["id"]: s.get("clips", 0) for s in run.get("sources", [])}
    out = []
    for s in sources:
        if s.id.startswith("shared:"):
            continue
        was = before.get(s.id, 0)
        if s.clips != was:
            out.append({"id": s.id, "label": s.label, "before": was, "now": s.clips, "truth": s.truth})
    gone = {s.id for s in sources}
    out += [{"id": sid, "label": sid, "before": n, "now": 0, "truth": False} for sid, n in before.items()
            if sid not in gone and not sid.startswith("shared:") and n]
    return out


def describe(diff: list[dict]) -> str:
    parts = [f"{d['now'] - d['before']:+,} {d['label'].lower()}" for d in diff]
    return ", ".join(parts)


def guide(project: Path) -> dict:
    data = json.loads((project / "project.json").read_text(encoding="utf-8"))
    sources = [s for s in project_sources(project, keys=False) if not s.id.startswith("shared:")]
    positives = sum(s.clips for s in sources if s.truth)
    run = latest_run(project)
    diff = changes(sources, run) if run else []
    scans = [mining.summary(d) for d in mining.mine_dirs(project)]
    open_reviews = sum(s["counts"]["open"] for s in scans if s.get("state") != "running")
    scanned = bool(run and any(s.get("run") == run["id"] and s.get("state") == "done" for s in scans))
    # 1-2 mining rounds make a model robust; after that scans find hardly anything new (trainer/recipe.py)
    rounds = mining.rounds(project)
    mining_done = rounds >= recipe.MINING_ROUNDS
    synthetic = sum(s.clips for s in sources if s.id == "synthetic")
    # Once mining is done, a few more mined clips are no reason to train again (other new data still is)
    retrain = bool(diff) and not (mining_done and all(c["id"].startswith("mined-") for c in diff))
    exported = export.current(project)
    fresh = bool(run and (not exported or exported["run"] != run["id"]))
    active = jobs.active()
    mine = active if active and active["project"] == project.name else None
    steps: dict[str, dict] = {}

    def note(text: str, **params) -> dict:
        return {"attention": True, "note": fill(text, params), "tpl": text, "params": params}

    if run and retrain:
        steps["train"] = note("train again")
    if open_reviews:
        steps["mining"] = note("{n} to check", n=open_reviews)
    elif run and not scanned and not retrain and not mining_done:
        steps["mining"] = note("test the model")
    if fresh and not retrain:
        steps["export"] = note("new model")
    # Sound-alikes: words close to the wake word, spoken by the voice bank (the old model fired on half of them).
    words = data.get("confusables") or []
    spoken = set(corpus.load_index(corpus.confusable_dir(project)).get("phrases") or [])
    alike_clips = sum(s.clips for s in sources if s.id == "sound-alikes")
    unspoken = [w for w in words if w not in spoken]
    if not words:
        steps["wakeword"] = note("add sound-alikes")
    if unspoken or (words and not alike_clips):
        steps["generate"] = note("{n} sound-alikes to generate", n=len(unspoken) or len(words))
    elif positives and (synthetic < recipe.MIN_POSITIVES or alike_clips < recipe.MIN_SOUND_ALIKES):
        steps["generate"] = note("below the recommended amount")
    # Phrases of the false triggers that would make good sound-alikes; offered in the mining step until taken or
    # turned down (project.json "confusables_rejected").
    offered = confusables.mined_candidates(project, data.get("tts_phrases") or [data["wake_word"]], data["language"],
                                           [*words, *(data.get("confusables_rejected") or [])]) if scans and not mining_done else []
    if offered and not open_reviews:
        steps["mining"] = note("{n} sound-alikes to pick", n=len(offered))

    def nxt(step: str, title: str, text: str, action: str | None = None, **params) -> dict:
        """Texts are templates with {name} placeholders: the UI translates the template and fills in the params."""
        return {"step": step, "title": fill(title, params), "text": fill(text, params), "action": action,
                "tpl": {"title": title, "text": text}, "params": params}

    ready = setup.quick()
    if not ready["training"] or not ready["generation"]:
        missing = [k for k in ("training", "generation") if not ready[k]]
        what = {("training",): "training", ("generation",): "sample generation"}.get(tuple(missing), "training and sample generation")
        rec = nxt("setup", "Download the shared data first",
                  f"This machine is missing data for {what} (speech and noise datasets, voices, models; about 50 GB the "
                  "first time). The data page downloads and prepares everything in one go.", "Open the data page")
    elif mine:
        step = KIND_STEP.get(mine["kind"], "train")
        rec = nxt(step, KIND_LABEL.get(mine["kind"], "A job is running"),
                  "You can leave this page; progress shows in the top bar. The next step appears here when it is done.")
    elif not positives:
        rec = nxt("generate", "Generate synthetic samples",
                  "Thousands of cloned voices saying the wake word are the base of every model. Check the pronunciation "
                  "first, then generate {min} clips (about 1.5-2 hours).", "Generate samples", min=recipe.MIN_POSITIVES)
    elif not alike_clips and not words:
        rec = nxt("wakeword", "Add sound-alike words",
                  "Words that sound close to the wake word teach the model what to ignore; without them it also fires on "
                  "similar words. Suggest in the wake word step finds them in a few seconds.", "Find sound-alikes")
    elif not alike_clips:
        rec = nxt("generate", "Generate the sound-alike clips",
                  "{n} sound-alike words are set but not spoken yet. The voices say each of them (about 20 minutes for "
                  "{min} clips), so the model learns to tell them apart from the wake word.", "Generate sound-alikes",
                  n=len(words), min=recipe.MIN_SOUND_ALIKES)
    elif synthetic < recipe.MIN_POSITIVES:
        rec = nxt("generate", "Generate at least {min} wake word samples",
                  "There are {n} synthetic samples. Models trained with {min} or more held up much better on the device. "
                  "Generation tops the corpus up, so it continues where it stopped.", "Generate samples",
                  n=synthetic, min=recipe.MIN_POSITIVES)
    elif alike_clips < recipe.MIN_SOUND_ALIKES:
        rec = nxt("generate", "Generate at least {min} sound-alike clips",
                  "There are {n} sound-alike clips. With {min} or more the model learns to ignore words that sound "
                  "like the wake word.", "Generate sound-alikes", n=alike_clips, min=recipe.MIN_SOUND_ALIKES)
    elif not run:
        rec = nxt("train", "Train your first model",
                  "{positives} wake word clips are ready. A fast run takes about 20 minutes and already gives a model to "
                  "try; take thorough (about 40 minutes) for the one to keep.", "Train a model", positives=positives)
    elif open_reviews:
        rec = nxt("mining", "Check {n} triggers",
                  "The automatic check could not decide these. Listen and label them; afterwards train again so the "
                  "model learns from them.", "Review triggers", n=open_reviews)
    elif offered:
        rec = nxt("mining", "Turn {n} false triggers into sound-alikes",
                  "These phrases triggered the model in real speech and sound close to the wake word. As sound-alikes "
                  "the voices speak each of them hundreds of times, which teaches the model more than the few real "
                  "clips. Pick them in the mining step, then generate their clips and train again.",
                  "Pick sound-alikes", n=len(offered))
    elif unspoken:
        # Before "train again": one run then learns from the new mined clips and the new sound-alikes together.
        rec = nxt("generate", "Generate clips for the new sound-alikes",
                  "{n} sound-alike words were added after the last generation and are not spoken yet. Top up the "
                  "sound-alike clips, then train again.", "Generate sound-alikes", n=len(unspoken))
    elif retrain:
        rec = nxt("train", "Train again: the model does not know the new data yet",
                  "Since the last model ({run}): {changes}. Clips only take effect in a new training run; then scan again "
                  "to see whether the false triggers went down.", "Train again", run=run["id"], changes=describe(diff))
    elif not scanned and not mining_done:
        rec = nxt("mining", "Test the model on real speech",
                  "Mining runs the newest model over hours of speech and collects what triggers it. The clear false "
                  "triggers are labeled automatically; feeding them back makes the model much more robust. One or two "
                  "rounds (scan, review, train again) are enough, after that it hardly improves. This is round {round}.",
                  "Scan for false triggers", round=rounds + 1)
    elif fresh:
        last = next((s for s in scans if s.get("run") == run["id"]), None)
        faph = last.get("faph") if last else None
        if not scanned:
            rec = nxt("export", "Export the newest model",
                      "{rounds} mining rounds are done; more rounds hardly improve a model. Pick its operating point, "
                      "export it for ESPHome and test it on the device.", "Export the model", rounds=rounds)
        elif faph is not None:
            rec = nxt("export", "Export the newest model",
                      "The newest model has been scanned ({faph} false triggers per hour of speech) and every trigger is "
                      "reviewed. Pick its operating point and export it for ESPHome.", "Export the model",
                      faph=round(faph, 2))
        else:
            rec = nxt("export", "Export the newest model",
                      "The newest model has been scanned and every trigger is reviewed. Pick its operating point and "
                      "export it for ESPHome.", "Export the model")
    else:
        rec = nxt("export", "The model is up to date",
                  "The newest model is exported (cutoff {cutoff}). Flash it to the device and try it; if it fires on "
                  "something specific, record that room or put such audio into a mining folder and scan it.",
                  "Open the export", cutoff=exported["probability_cutoff"])
    return {"next": rec, "steps": steps, "changes": diff, "run": run["id"] if run else None,
            "export": exported["id"] if exported else None, "wake_word": data.get("wake_word")}


def fill(template: str, params: dict) -> str:
    return template.format(**{k: f"{v:,}" if isinstance(v, int) else v for k, v in params.items()})
