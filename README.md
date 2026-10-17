# Wakeforge

> **Note:** This project is "vibe coded" — the code was written iteratively with AI assistance.

A lean, self-maintained training pipeline for custom [microWakeWord](https://github.com/kahrendt/microWakeWord)
models (ESPHome on-device wake words). It replaces the TaterTotterson trainer container for
day-to-day use.

Goals:

- Natural positive samples: voice-cloning TTS (OmniVoice) with hundreds of real reference voices, checked phonetically.
- Hard-negative mining: scan negative audio for false triggers and retrain with them.
- Content-hash caching per input set, so new personal or negative samples are always picked up.
- Profiles (`fast`, `thorough`).
- Output: ESPHome-valid manifest (`.json`) + `.tflite`.

Status: early. Web UI with projects, setup wizard, synthetic samples, sound-alikes, Sample Studio and training
(featurize, train, calibrate, ESPHome manifest + `.tflite` per run) and mining work; the export page is next.
The UI switches between English and German (top bar).

## Languages

Wake words are **German only for now** (decision 2026-10-16). The pipeline is built language-aware, but only German
has the data and calibration behind it:

| Part | Languages | State |
|---|---|---|
| TTS (OmniVoice) | 646 | works for any language |
| Phonetic check (wav2vec2 phones + espeak-ng) | ~100 | works; thresholds calibrated on German only |
| Whisper (mining check) | 99 | works |
| Training, noise negatives, calibration | language independent | works |
| Voice bank for cloning (`trainer/tts/voices.py`) | German (Kartoffelphon) | other languages fall back to designed voices only (less variety, untested) |
| Speech corpus for mining (`trainer/mining/audio.py`) | German (Kartoffelphon) | other languages: own audio folders only |
| Sound-alike suggestions (`trainer/tts/confusables.py`) | letter rules for de/en | others: words typed in by hand |

Adding a language properly needs: a voice bank source (e.g. Multilingual LibriSpeech or Common Voice, read by
`voices.py` like Kartoffelphon), a speech corpus to mine (ideally the same source, added to `trainer/setup.py`), and a
check of the phonetic thresholds against a few real recordings. English would be the natural next one.

## Web UI workflow

Everything is organised per wake word ("project"). The start page lists all projects; "New wake word"
opens a four-page wizard (name + language, TTS spellings, manifest credits, review). Inside a project
a step bar guides through the workflow, every step stays clickable and shows its status:

1. Wake word: settings (name, language, TTS spellings, author, website) and sound-alikes (`trainer/tts/confusables.py`):
   words at phone distance 0.35-0.65 from the spellings, suggested from letter edits, voice bank transcripts and the
   closest words of the project's mined false triggers (those first). The wizard fills them in by itself (remove what
   you do not want), an empty list in step 1 shows suggestions right away, and the guide asks for them before the first
   training and whenever words are not spoken yet.
2. Synthetic samples: OmniVoice clones a random voice from the voice bank (or designs one from speaker
   attributes) per clip, with random tempo and punctuation. A multilingual phone recognizer
   (`facebook/wav2vec2-xlsr-53-espeak-cv-ft`) transcribes every clip into IPA and `trainer/tts/phonetic.py`
   compares it with the espeak-ng pronunciation of the spellings (weighted phone edit distance, onset x2.5,
   vocalized r cheap): Δ ≤ 0.30 positive, Δ ≥ 0.70 (and wake word not contained, ≥ 3 phones) negative
   candidate, gray zone dropped (~30 %). Background job with live progress, stop/continue and random
   listening for both sets. ~3-4 positive clips/s on the RTX 3090 (20k clips in ~1.5-2 h)

The pronunciation check (2 voices per spelling, phones + Δ) lives on the wake word page and in the wizard,
so spellings are verified before generating (`POST /api/tts/preview`, no project needed).
3. Own recordings: Sample Studio (optional, recommended)
4. Training (`trainer/train/`): one background job per run with live phases.
   - Sources (`sources.py`): synthetic positives (weight 2), own positives (3), sound-alikes (6, penalty 1.25),
     own negatives (8, 1.25), mined false triggers (8, 1.25), mined real wake words (3), borrowed projects (4),
     shared VOiCES speech (12), CHiME-6 dinner party (12),
     no-speech (5), CHiME-6/DiPCo ambient for validation and test. Clips split 80/10/10 by a hash of their name.
   - Features (`featurize.py`): microWakeWord augmentation (RIR, background noise, EQ, ...) in a CPU pool, stored
     as uint16 (lossless, half of float32) and cropped to the 178 frames the model reads. Cached per corpus shard /
     WAV folder in `data/cache/features/<key>/`, keyed by content + feature version, shared across projects.
   - Training: microWakeWord `model_train_eval` (mixednet as in the old trainer, batch 32, lr 0.001 → 0.0003 → 0.0001
     over 60/25/15 % of the steps, negative class weight 20) on the GPU (`mww_entry.py` refuses a CPU-only TF), profiles fast (10k steps) / thorough (40k), then the
     streaming int8 TFLite export.
   - Calibration (`calibrate.py`): the quantized streaming model runs over the test split and ~5 h ambient audio;
     cutoff + sliding window are picked for a target of false accepts per hour (default 0.25) with the old trainer's
     tier rule; recall per positive source and trigger rate per word-like negative source are reported.
   - Output per run in `projects/<slug>/runs/<id>/`: `<wake_word>.tflite`, `<wake_word>.json` (ESPHome manifest,
     allowed keys only), `run.json` (sources, validation history, calibration curve), `mww/` (checkpoints, logs).
5. Mining (`trainer/mining/`, optional): a background job streams a run's int8 TFLite model over hours of real
   audio on the CPU, exactly like the device (microfrontend via pymicro_features, 2 slices per invoke, window average,
   cooldown; scores match `calibrate.py` to 1e-7), with 12 worker processes and no TF import, ~1800x realtime.
   - Audio (`audio.py`): Kartoffelphon (German projects; 11,894 utterances, 33.8 h incl. pauses, with transcripts)
     and any folder `datasets/mining/<name>/` with wav/flac/mp3/ogg. Utterances are scanned as continuous streams of
     64 with 0.3 s pauses, so frontend and model state run through like on the device; triggers in the first 1.5 s
     of a stream are ignored. A fresh frontend per utterance tripled the triggers at utterance starts.
   - Triggers at the run's cutoff (or lower, "near misses") are kept with 4.5 s context, score, transcript and a flag
     when the transcript contains the wake word. Review in the UI (keys N/P/X): "not it" writes the 2.3 s training cut
     to `mining/negatives/`, "wake word" to `mining/positives/`; both are training sources of the next run.
   - Automatic check (`triage.py`, OmniVoice venv, GPU), run by the scan job after the scan (or later per scan):
     the IPA phone recognizer on each training cut ("contains the wake word" distance) and the transcript, or Whisper
     large-v3-turbo on the context for sources without one ("closest one/two-word sequence" by espeak-ng phones).
     Either at <= 0.30 flags the trigger for review; everything else is labeled "not it" automatically when the scan
     was declared free of the wake word (default; off for own rooms). Triggers at a place another scan already put
     into training are skipped as duplicates. Neither signal alone could tell a real wake word apart reliably: of the
     98 own recordings spliced into Kartoffelphon speech the phones caught ~89 %, Whisper less than half (it writes
     the context words or hallucinates), so the automatic negative also relies on the source.
   - First scan, thorough run `20261016-132045-thorough` (0.19 false accepts/h on the ambient set): 234 triggers in
     33.8 h of Kartoffelphon, 6.9 false accepts/h on German speech; the check labeled 228 and flagged 6 (all
     harmless: "ja wie", "ja würde", English sentences) in 27 s.
6. Export (`trainer/export.py`): pick a finished run and one of its calibration points (cutoff); the UI shows recall,
   false accepts/h on the ambient set and, if the run was mined, on real speech (trigger scores of the scan above
   the chosen cutoff, triggers labeled as the wake word left out; only cutoffs >= the scan's cutoff are known).
   - Output in `projects/<slug>/models/<YYYYMMDD-HHMMSS>/`: the run's `.tflite` unchanged, the ESPHome manifest with
     the chosen `probability_cutoff` and the project's current name, author and website (edited on the export page),
     `export.json` (run, operating point, metrics). The newest export is current.
   - Into ESPHome: the .zip (manifest + model) into `esphome/wakeforge/` and `model: wakeforge/<slug>.json`, or both
     files pushed to a public Git repo and `model: https://raw.githubusercontent.com/<owner>/<repo>/main/models/<slug>.json`,
     or the
     stable URL `/api/projects/<slug>/models/current/<slug>.json` (GET + HEAD, needs `WAKEFORGE_BIND`, see below),
     which ESPHome checks for changes on every compile.

The workflow is a loop (generate -> train -> mine -> train again -> mine again), so a guide (`trainer/guide.py`,
`GET /api/projects/<slug>/guide`) derives the one next thing to do from the files: a running job, missing samples,
a first model, triggers to check, a model older than its training data (clip counts per source vs. the newest run's
`run.json`), a model not scanned yet. Every project page shows it as a "Next step" card, the step bar marks steps that
need attention (e.g. Training "train again"), the footer and the project list follow it, and opening a project jumps
to it. Each step page starts with a short "how" text (`ui/src/lib/steps.ts`).

## Layout

| Path | Purpose |
|---|---|
| `docker/Dockerfile` | Image with two venvs: `/opt/venv-train` (TF 2.21 + microWakeWord, CUDA 12 pip libs) and `/opt/venv-omni` (OmniVoice + wav2vec2 check, PyTorch, CUDA 13 pip libs) |
| `docker/requirements-*.in` | Direct dependencies (exact pins) |
| `docker/requirements-*.txt` | Fully resolved lock files (generated, see below) |
| `spikes/` | Throwaway experiments with measurements |
| `trainer/` | Python package: projects (`trainer/projects.py`), Sample Studio storage (`trainer/studio/`), FastAPI server (`trainer/server/`), pipeline (WIP) |
| `ui/` | Web UI "Wakeforge" (Vite + Svelte 5 + TypeScript) |
| `data/` | All datasets, caches, models and runs. Git-ignored, bind-mounted to `/app/data` |

Data layout below `data/`:

| Path | Content |
|---|---|
| `projects/<slug>/project.json` | Project settings: `wake_word`, `language`, `tts_phrases`, `confusables` (sound-alike negatives, see `trainer/tts/confusables.py`), `author`, `website`, `borrow_negatives` (other projects whose recordings and/or synthetic clips count as negatives here; referenced, not copied; refused when the wake words are within phone distance 0.35, see `trainer/projects.py`) |
| `projects/<slug>/recordings/<YYYYMMDD-HHMMSS>/` | Sample Studio takes: `recording.wav`, `clips/`, `clips.json`, `session.json` |
| `projects/<slug>/samples/positive/`, `.../negative/` | Sorted real clips (16 kHz mono PCM16). A clip's label is the folder holding its copy |
| `projects/<slug>/generated/tts/` | Synthetic corpus: `corpus.json` index + `shard-*.npz` (int16 clips concatenated, offsets, voice, phrase, speed/design/steps), see `trainer/tts/corpus.py` |
| `projects/<slug>/generated/tts-negative/` | Negative candidates: TTS clips that came out clearly different from every spelling (same format) |
| `projects/<slug>/generated/tts-confusable/` | Sound-alike negatives: the `confusables` spoken by the voice bank, minus clips that sounded like (or contained) the wake word (same format) |
| `cache/preview/<key>/` | Pronunciation check clips (last 20 checks) |
| `projects/<slug>/jobs/<YYYYMMDD-HHMMSS>-<kind>/` | Background job: `job.json` (command, pid), `progress.json` (written by the worker), `log.txt` |
| `projects/<slug>/runs/<YYYYMMDD-HHMMSS>-<profile>/` | Training run: model, manifest, `run.json`, `training_parameters.yaml`, `mww/` train dir |
| `projects/<slug>/mining/<YYYYMMDD-HHMMSS>/` | Mining scan: `mine.json` (model, cutoffs, sources, hours, auto), `hits.json`, `clips/<n>.wav` (context), `triage.json` (automatic check), `labels.json` + `auto.json` (review; labels the check set) |
| `projects/<slug>/mining/negatives/`, `.../positives/` | Reviewed triggers (training cuts, 16 kHz PCM16), training sources `mined-negative` / `mined-positive` |
| `datasets/mining/<name>/` | Own audio to mine (any wav/flac/mp3/ogg, read in 10 min blocks) |
| `cache/features/<key>/` | Cached spectrograms per corpus shard or WAV folder (`training/`, `validation/`, `testing/` RaggedMmaps + `done`) |
| `datasets/` | Shared negative feature sets (`negative_datasets/`) and augmentation audio (`mit_rirs_16k/`, `audioset_16k/`, `fma_16k/`, `chime_16k/`), prepared by `trainer.setup` |
| `downloads/setup/` | Partial downloads of the setup (`*.part`, resumed on the next run; archives are deleted after unpacking) |
| `system/jobs/` | Jobs that belong to no project (the setup) |
| `voices/<language>/` | Voice bank for cloning: `<id>.wav` (2.5-5 s, 24 kHz, cut at pauses) + `voices.json` (transcript, F0, gender, source). German: 300 voices (150 m / 150 f) from Kartoffelphon |
| `downloads/kartoffelphon/` | Kartoffelphon Arrow shards `test-NNNNN.arrow` (HF `MultiLlasa/Kartoffelphon-2.5M-de-ger`, test split, German podcasts + LibriVox, CC-BY 4.0): shard 0 is the voice bank source, every shard ~33 h of speech to mine |

`python -m trainer.projects list` shows all projects. `python -m trainer.projects migrate --wake-word X`
moves a pre-project `data/recordings` + `data/samples` layout into a new project (already done for Dscharwis).

## Usage

```sh
cd ui && npm ci --before=YYYY-MM-DD && npm run build && cd ..
docker compose up -d --build
```

### A new machine

`data/` is not in the repository. On a fresh machine open the **Data** page (top bar; the home page and the
"Next step" card point there while something is missing) and start the download, or run
`py-train -m trainer.setup run --job /tmp/setup` in the container (`status` lists what is there). `trainer/setup.py`
downloads everything from pinned sources with SHA-256 checks, resumes interrupted downloads, streams archives member
by member (extra disk: one archive) and converts audio exactly like the old trainer's `setup_*` scripts (librosa,
16 kHz mono int16), so the result is the same data:

| Item | Source (pinned) | Download | On disk |
|---|---|---|---|
| negatives | HF `kahrendt/microwakeword` @0da95f9: speech, dinner_party, no_speech, dinner_party_eval zips | 5.7 GB | 25 GB |
| rirs | HF `TaterTotterson/MIT_environmental_impulse_responses` @1b3e09d, `16khz/` | 8 MB | 6 MB |
| audioset | HF `agkphysics/AudioSet` @6762f04, `data/bal_train00-09.tar` (FLAC) | 26 GB | 5.6 GB |
| fma | `os.unil.cloud.switch.ch/fma/fma_small.zip` (MP3) | 7.7 GB | 7.2 GB |
| chime | `archive.org/download/chime-home/chime_home.tar.gz` (48 kHz WAV) | 4.2 GB | 0.8 GB |
| speech | HF `MultiLlasa/Kartoffelphon-2.5M-de-ger` @5100918, `test/data-0000N-of-00011.arrow` (1-11 shards) | 0.5 GB each | same |
| models | OmniVoice, wav2vec2 phones, Whisper at the revisions in `trainer/models.py` (OmniVoice venv) | 6.2 GB | same |
| voices | built from speech shard 0 with `trainer.tts.voices build` (GPU, seed 0, shard 0 only) | - | 60 MB |

Projects move as a .zip: Data page -> Export (or `py-train -m trainer.setup export <slug>`), then Import on the
other machine (`... setup import <file.zip> [--slug name]`). Everything but `jobs/` moves along.

The container starts the API and UI on http://localhost:8790 (uvicorn with auto-reload on `trainer/`).
For UI development run `npm run dev` in `ui/` and open http://localhost:5173 (proxies `/api` to 8790).
The port listens on 127.0.0.1 only. `WAKEFORGE_BIND=0.0.0.0` in `.env` opens it to the LAN, so ESPHome can fetch the
current exported model from `http://<pc>:8790/api/projects/<slug>/models/current/<slug>.json` on every compile
(the API has no login, so only in a trusted network).

Shell into the container:

```sh
docker exec -it wakeforge bash
```

Inside the container, use the launchers `py-omni` (OmniVoice venv) and `py-train` (TF venv):

```sh
py-omni -m trainer.tts.voices build --language de --max 300     # voice bank, once per language
py-omni -m trainer.tts.omni run --project data/projects/dscharwis --job /tmp/job --count 20000
py-omni -m trainer.tts.omni preview --project data/projects/dscharwis --phrase Jarvis --out /tmp/preview
py-train -m microwakeword.model_train_eval --training_config spikes/train_smoke.yaml ... mixednet ...
```

## Spike results (RTX 3090, 2026-10-15)

| Stage | Result |
|---|---|
| OmniVoice, `trainer.tts.omni` (batch 32, 16 steps, 2.5-5 s refs, IPA phone check) | ~3-4 positive clips/s, ~30 % gray zone, ~0.5 % negative candidates. TTS is ~98 % of the time; Whisper as the check was 75x slower than wav2vec2 CTC (30 s padding) and guessed real words |
| Sound-alikes, `trainer.tts.omni --mode confusable` (24 words, batch 32, 16 steps) | ~4.4 clips/s, ~5 % dropped as too close to the wake word; suggestions (letter edits + 3.6k voice-bank words, espeak in one batch) ~4 s |
| Phone check calibration (`spikes/phonetic_check.py`) | OmniVoice 79 % positive / 0 % negative, own recordings median Δ 0.24, random German speech 0 % positive / 88 % negative, "Garvis" 0.33 / "Davis" 0.44 gray |
| Featurize 2k clips, 14 workers | 75 s (1600 train clips x2 reps x10 slides = 32k spectrograms, 1.5 GB float32) |
| Train 500 steps + streaming int8 TFLite export | ~3 min total |
| `trainer.train` featurize, warm pool (2026-10-16) | 17.7k new clips in 2.7 min (111 clips/s), cache 4.7 GB for 20k + 300 sound-alikes + 98 own |
| `trainer.train` thorough, 20k synthetic + 98 own + 300 sound-alikes | 40k steps ~35 min (18-19 steps/s incl. validation), run total 40 min. Test: recall 94.0 % at 0.19 FA/h (cutoff 0.86, window 8), sound-alikes trigger 10.7 % |
| Old Tater model (50k clips) on the same test set | recall 91.4 % at 0.19 FA/h (cutoff 0.98, window 3), sound-alikes trigger 53.6 % |
| `trainer.train` fast, 2.3k synthetic only | 10k steps, run total 20 min, recall 71 % at 0.19 FA/h |

### Spike B: German TTS quality (2026-10-15)

`spikes/ttsb/` compares voice-cloning models with 10 German reference voices from Kartoffelphon
(`refs.py`), one venv per model under `/opt/spike/<name>` in the container, Whisper large-v3-turbo
scoring (`score_asr.py`: spelling regex or Kölner Phonetik match) and a listening page (`report.py`
-> `data/spikes/listen/index.html`).

| Model | Whisper hits | Clips/s (3090) | VRAM |
|---|---|---|---|
| OmniVoice clone (chosen, best by ear) | 29-30/30 | 1.3; 6.0 with 3 s refs + 16 steps | 5-9 GB |
| Qwen3-TTS 1.7B Base clone | 29/30 | 2.3-3.0 (HF generate) | 9-19 GB |
| Audio8 TTS 0.6B clone | 30/30 | 2.5-3.0 | 10-19 GB |
| OmniVoice voice design | 18/18 | 6.7 | 2 GB |
| VoxCPM2 clone + design | 37/48 | 1.0 (no batching) | 6 GB |

Batch size beyond 16 does not raise throughput for any of them; reference length and diffusion steps do.

Open optimizations: cache background audio in RAM, store spectrograms as uint16 (half size),
fewer stored slide frames.

## Dependencies

Pin exact versions released at least 7 days ago. Lock files are generated with an
`--exclude-newer` cut-off date (today minus 7 days):

```sh
uv pip compile docker/requirements-train.in -o docker/requirements-train.txt \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 --exclude-newer YYYY-MM-DD
uv pip compile docker/requirements-omni.in -o docker/requirements-omni.txt \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 --exclude-newer YYYY-MM-DD
```

## Notes

- The CUDA pip libs are not found automatically by TF (`libcusolver.so.11`; onnxruntime-gpu had the same
  issue with `libcublasLt.so.13`). The `py-train`/`py-omni` launchers set `LD_LIBRARY_PATH` per venv.
- microWakeWord's `audio/` and `layers/` have no `__init__.py`, so a plain pip install drops them;
  the Dockerfile clones the pinned commit and adds them. TensorBoard is required for `tf.summary`.
- ESPHome manifests may only contain: `type`, `wake_word`, `author`, `website`, `model`,
  `trained_languages`, `version` (2) and `micro{probability_cutoff, sliding_window_size,
  feature_step_size, tensor_arena_size, minimum_esphome_version}`.
