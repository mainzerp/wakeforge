"""Wake word projects: one directory per wake word below PROJECTS.

  <slug>/project.json   settings (wake word, language, TTS spellings, sound-alike negatives, manifest author/website)
  <slug>/recordings/    Sample Studio takes
  <slug>/samples/       sorted real clips (positive/, negative/)
  <slug>/generated/     synthetic positives (TTS corpus shards, previews)
  <slug>/jobs/          pipeline job state and logs
  <slug>/runs/          training runs
  <slug>/models/        exported models

Shared datasets (negatives, RIRs, background noise) stay global below DATA/datasets.

A project can borrow other projects' clips as negatives ("borrow_negatives" in project.json): their own recordings
(positive and negative) and/or their synthetic positives. They are referenced, not copied, so later changes in the
other project carry over. Projects whose wake word sounds like this one are refused, their clips would contradict
this project's positives.

  python -m trainer.projects migrate --wake-word Dscharwis --language de
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from trainer.paths import LEGACY_RECORDINGS, LEGACY_SAMPLES, PROJECTS
from trainer.studio.splitter import sanitize_prefix
from trainer.tts import confusables, corpus, phonetic

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LANGUAGE_RE = re.compile(r"^[a-z]{2}$")
SETTINGS = ("wake_word", "language", "tts_phrases", "author", "website", "borrow_negatives", "confusables",
            "confusables_rejected")
MAX_CONFUSABLES = 200
MAX_REJECTED = 2000
BORROW_KINDS = ("recordings", "synthetic")
# Phone distance between the two wake words (see phonetic.word_distance). Up to CONFLICT_MAX the other project's
# clips would teach contradicting labels ("Jarvis" vs. "Hey Jarvis") and are refused. Up to CLOSE_MAX they are
# only flagged: those are the most useful hard negatives ("Marvin", "Davis").
CONFLICT_MAX = phonetic.CONFLICT_MAX
CLOSE_MAX = 0.55


def slugify(text: str) -> str:
    slug = sanitize_prefix(text).lower()
    if not slug:
        raise ValueError("wake word needs at least one letter or digit")
    return slug


def project_dir(slug: str) -> Path:
    if not SLUG_RE.match(slug or ""):
        raise ValueError(f"invalid project: {slug!r}")
    path = PROJECTS / slug
    if not (path / "project.json").is_file():
        raise FileNotFoundError(f"project not found: {slug}")
    return path


def clean_settings(raw: dict) -> dict:
    settings = {k: raw[k] for k in SETTINGS if raw.get(k) is not None}
    if "wake_word" in settings:
        settings["wake_word"] = settings["wake_word"].strip()
        if not settings["wake_word"]:
            raise ValueError("wake word must not be empty")
    if "language" in settings and not LANGUAGE_RE.match(settings["language"]):
        raise ValueError(f"invalid language code: {settings['language']!r}")
    if "tts_phrases" in settings:
        settings["tts_phrases"] = list(dict.fromkeys(p.strip() for p in settings["tts_phrases"] if p.strip()))
    for key in ("author", "website"):
        if key in settings:
            settings[key] = settings[key].strip()
    if "confusables" in settings:
        words = list(dict.fromkeys(w.strip() for w in settings["confusables"] if w.strip()))
        if len(words) > MAX_CONFUSABLES or any(len(w) > 60 for w in words):
            raise ValueError(f"at most {MAX_CONFUSABLES} sound-alikes of 1-60 characters")
        settings["confusables"] = words
    if "confusables_rejected" in settings:
        settings["confusables_rejected"] = list(dict.fromkeys(
            w.strip() for w in settings["confusables_rejected"] if w.strip()))[-MAX_REJECTED:]
    if "borrow_negatives" in settings:
        borrow = {}
        for entry in settings["borrow_negatives"]:
            if not SLUG_RE.match(str(entry.get("slug", ""))):
                raise ValueError(f"invalid project: {entry.get('slug')!r}")
            kinds = {k: bool(entry.get(k)) for k in BORROW_KINDS}
            if any(kinds.values()):
                borrow[entry["slug"]] = {"slug": entry["slug"], **kinds}
        settings["borrow_negatives"] = list(borrow.values())
    return settings


def count_files(path: Path, pattern: str = "*.wav") -> int:
    return sum(1 for _ in path.glob(pattern)) if path.is_dir() else 0


def count_dirs(path: Path) -> int:
    return sum(1 for d in path.iterdir() if d.is_dir()) if path.is_dir() else 0


def own_counts(path: Path) -> dict:
    return {
        "positive": count_files(path / "samples" / "positive"),
        "negative": count_files(path / "samples" / "negative"),
        "sessions": count_dirs(path / "recordings"),
        "generated": corpus.load_index(corpus.corpus_dir(path))["total"],
        "confusable": corpus.load_index(corpus.confusable_dir(path))["total"],
        "runs": sum(1 for _ in (path / "runs").glob("*/*.tflite")) if (path / "runs").is_dir() else 0,
        "models": count_files(path / "models", "*/*.tflite"),
        "mined": count_files(path / "mining" / "negatives"),
    }


def summary(path: Path, data: dict) -> dict:
    return {**own_counts(path), "borrowed": sum(s["clips"] for s in borrowed_sources(data))}


def read(path: Path) -> dict:
    return json.loads((path / "project.json").read_text(encoding="utf-8"))


def load(path: Path) -> dict:
    data = read(path)
    return {**data, "slug": path.name, "borrow_negatives": data.get("borrow_negatives", []),
            "confusables": data.get("confusables", []), "confusables_rejected": data.get("confusables_rejected", []),
            "summary": summary(path, data)}


def similarity(a: dict, b: dict) -> dict:
    d = phonetic.word_distance(a.get("tts_phrases") or [a["wake_word"]], a["language"],
                               b.get("tts_phrases") or [b["wake_word"]], b["language"])
    return {"distance": d, "level": "conflict" if d <= CONFLICT_MAX else "close" if d <= CLOSE_MAX else "ok"}


def borrowed_sources(data: dict) -> list[dict]:
    """The clip sets a project borrows as negatives, re-checked against the current wake words.

    Each entry: {slug, kind: recordings|synthetic, dirs: [...], clips}. Recordings are WAV folders, synthetic is a
    corpus shard directory (trainer.tts.corpus). Missing or meanwhile conflicting projects are skipped.
    """
    out = []
    for entry in data.get("borrow_negatives", []):
        path = PROJECTS / entry["slug"]
        if not (path / "project.json").is_file():
            continue
        other = read(path)
        if similarity(data, other)["level"] == "conflict":
            continue
        if entry.get("recordings"):
            dirs = [path / "samples" / label for label in ("positive", "negative")]
            out.append({"slug": entry["slug"], "kind": "recordings", "dirs": dirs, "clips": sum(count_files(d) for d in dirs)})
        if entry.get("synthetic"):
            out.append({"slug": entry["slug"], "kind": "synthetic", "dirs": [corpus.corpus_dir(path)],
                        "clips": corpus.load_index(corpus.corpus_dir(path))["total"]})
    return out


def borrow_candidates(slug: str) -> list[dict]:
    """All other projects with their clip counts, how close their wake word is and what is borrowed from them."""
    data = read(project_dir(slug))
    chosen = {e["slug"]: e for e in data.get("borrow_negatives", [])}
    out = []
    for path in sorted(PROJECTS.iterdir()):
        if path.name == slug or not (path / "project.json").is_file():
            continue
        other, counts = read(path), own_counts(path)
        out.append({
            "slug": path.name, "wake_word": other["wake_word"], "language": other["language"],
            "recordings": counts["positive"] + counts["negative"], "synthetic": counts["generated"],
            **similarity(data, other),
            "selected": {k: bool(chosen.get(path.name, {}).get(k)) for k in BORROW_KINDS},
        })
    return out


def write(path: Path, data: dict) -> None:
    data = {k: v for k, v in data.items() if k not in ("slug", "summary")}
    (path / "project.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_projects() -> list[dict]:
    if not PROJECTS.is_dir():
        return []
    projects = [load(d) for d in PROJECTS.iterdir() if (d / "project.json").is_file()]
    return sorted(projects, key=lambda p: p.get("updated", ""), reverse=True)


def create(raw: dict) -> dict:
    settings = clean_settings(raw)
    if "wake_word" not in settings:
        raise ValueError("wake word is required")
    base = slugify(settings["wake_word"])
    PROJECTS.mkdir(parents=True, exist_ok=True)
    for i in range(1, 100):
        path = PROJECTS / (base if i == 1 else f"{base[:36]}-{i}")
        try:
            path.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise ValueError(f"too many projects named {base!r}")
    now = datetime.now().isoformat(timespec="seconds")
    data = {"language": "de", "author": "", "website": "", **settings, "created": now, "updated": now}
    data.setdefault("tts_phrases", [data["wake_word"]])
    write(path, data)
    return load(path)


def update(slug: str, raw: dict) -> dict:
    path = project_dir(slug)
    data = load(path)
    before = data["confusables"]
    data.update(clean_settings(raw))
    # Sound-alikes taken off the list count as turned down (mining stops offering them); listed ones never are.
    listed = {w.lower() for w in data["confusables"]}
    dropped = [w for w in before if w.lower() not in listed]
    data["confusables_rejected"] = [w for w in dict.fromkeys([*data["confusables_rejected"], *dropped])
                                    if w.lower() not in listed][-MAX_REJECTED:]
    # Only a changed selection is validated; a wake word renamed into a conflict just makes borrowed_sources skip it.
    for entry in data["borrow_negatives"] if raw.get("borrow_negatives") is not None else []:
        if entry["slug"] == slug:
            raise ValueError("a project cannot borrow its own clips")
        other = PROJECTS / entry["slug"]
        if not (other / "project.json").is_file():
            raise FileNotFoundError(f"project not found: {entry['slug']}")
        if similarity(data, read(other))["level"] == "conflict":
            raise ValueError(f"the wake word of {entry['slug']!r} sounds too much like this one to use its clips as negatives")
    if raw.get("confusables"):
        scores = confusables.score(data["confusables"], data["tts_phrases"], data["language"])
        close = [w for w, d in zip(data["confusables"], scores) if d <= CONFLICT_MAX]
        if close:
            raise ValueError(f"these sound like the wake word itself, not like a negative: {', '.join(close)}")
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    write(path, data)
    return load(path)


def touch(path: Path) -> None:
    data = load(path)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    write(path, data)


def migrate_legacy(wake_word: str, language: str) -> dict:
    """Move the pre-project recordings/ and samples/ into a new project."""
    if not LEGACY_RECORDINGS.is_dir() and not LEGACY_SAMPLES.is_dir():
        raise FileNotFoundError("nothing to migrate")
    project = create({"wake_word": wake_word, "language": language})
    path = PROJECTS / project["slug"]
    for src, dest in ((LEGACY_RECORDINGS, path / "recordings"), (LEGACY_SAMPLES, path / "samples")):
        if src.is_dir():
            shutil.move(src, dest)
    return load(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="move data/recordings and data/samples into a new project")
    migrate.add_argument("--wake-word", required=True)
    migrate.add_argument("--language", default="de")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "migrate":
        print(json.dumps(migrate_legacy(args.wake_word, args.language), indent=2, ensure_ascii=False))
    else:
        for p in list_projects():
            print(f"{p['slug']:24} {p['wake_word']:24} {p['language']}  {p['summary']}")


if __name__ == "__main__":
    main()
