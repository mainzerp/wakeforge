# Agent notes

- Talk to the user in German. Code, comments, commits and docs in English.
- Host: Windows 11, Docker Desktop (WSL2), RTX 3090, driver 591.86 (CUDA 13 capable).
  Everything large goes to `data/` (git-ignored) on F:.
- Run pipeline code inside the container: `docker compose up -d --build`, then
  `docker exec wakeforge py-train ...` (TF/microWakeWord) or `py-omni ...` (OmniVoice TTS + CTC check,
  PyTorch). Always use these launchers: they set LD_LIBRARY_PATH to the venv's CUDA pip libs. The venvs
  need different CUDA majors (12 vs 13), so never share LD_LIBRARY_PATH. The Piper venv was dropped.
- German TTS: Piper (all voices incl. MLS) is unusable for wake words; OmniVoice voice cloning won Spike B
  (`spikes/ttsb/`, results in README). Voice bank in `data/voices/<lang>/` (`py-omni -m trainer.tts.voices build`).
  Phonetic check of generated clips: IPA phone recognizer + espeak-ng targets in `trainer/tts/phonetic.py`
  (thresholds calibrated with `spikes/phonetic_check.py`; Whisper is ~75x slower on short clips because it pads
  to 30 s, and it guesses real words). Gray-zone clips are dropped, never labeled negative: near-misses as
  negatives would contradict the positives. Real confusable negatives should be generated on purpose
  (GraphemeAug-style spelling mutations, milestone 2). The user prefers the spelling "Jarvis".
- Spike venvs (`/opt/spike/{qwen,omni,voxcpm,audio8,asr}`) and `gcc` (Triton for Qwen) were installed into
  the running container only; they vanish on rebuild. OmniVoice does not need gcc.
- From Git Bash, prefix docker commands with `MSYS_NO_PATHCONV=1`, otherwise `/opt/...` paths get
  rewritten to `C:/Program Files/Git/opt/...`.
- GPU code must fail hard when CUDA is unavailable (never silently fall back to CPU).
- Image rebuilds take ~10 min (layer export of ~16 GB). For quick experiments, install extra pinned
  packages into the running container and add them to `docker/requirements-*.in` for the next build.
- Docker Desktop's disk lives on F: (`F:\DockerVolumes\DockerDesktopWSL`, set via `CustomWslDistroDir`
  in `%APPDATA%\Docker\settings-store.json`). The GUI move fails (robocopy exit 1 treated as error).
- Old trainer volume is `wake-word_mww-data` (not `mww-data`); mount it read-only only.
- Shared data comes from `trainer/setup.py` (pinned URLs, revisions, SHA-256, same conversion as the old trainer);
  model weights are pinned in `trainer/models.py` and loaded via `models.local(...)`. New external data or models
  must be added there, never fetched ad hoc, so a fresh machine can reproduce `data/` from the Data page.
- Dependency policy: exact pins, released >= 7 days ago. Regenerate lock files with
  `uv pip compile ... --exclude-newer <today-7d>` (see README). npm: `npm install --before=<today-7d>`.
- UI: `ui/` (Svelte 5 runes, TypeScript). Verify with `npm run check` and `npm run build` in `ui/`.
  The container serves `ui/dist` plus `/api` on port 8790; `npm run dev` serves on 5173 with a proxy.
  UI strings are written in English and wrapped in `t("...")` (`ui/src/lib/i18n.svelte.ts`, toggle EN/DE in the top bar);
  German goes into `ui/src/lib/i18n/de/<area>.ts`, `npm run i18n` lists missing keys. Server texts the UI shows are
  static English (or templates + params like `trainer/guide.py`) so the UI can translate them. Look: "Dark Neon / Synth" (tokens in `ui/src/app.css`).
- UI structure is project-first: start page = wake word list, "New wake word" = wizard, project page =
  step bar (`ui/src/lib/steps.ts`: wakeword, generate, record, train, mining, export). New features
  go into their step page under `ui/src/pages/steps/`; set `available: true` in `steps.ts` when a step
  works. The UI must explain itself: keep the step's `how` text in `steps.ts` current and teach `trainer/guide.py`
  the new state (it drives the "Next step" card, step bar attention, footer and project list). Hash routes: `#/`, `#/new`, `#/p/<slug>/<step>`. All per-wake-word data lives in
  `data/projects/<slug>/`; APIs are `/api/projects/...` and `/api/projects/<slug>/studio/...`.
- After a UI build the container picks up `ui/dist` immediately (no restart); uvicorn reloads on `trainer/` changes.
- The server runs in the TF venv and must not import torch/OmniVoice/Piper. GPU stages run as detached
  subprocess jobs via `trainer/jobs.py` (`py-omni -m ...` / `py-train -m ...`, one job at a time); the
  worker writes `progress.json` and handles SIGTERM as "stop after this batch". Follow this pattern for
  featurize/train/mining. The UI polls status once per second while a job runs.
- Training (`trainer/train/`): `py-train -m trainer.train.featurize --project ...` warms the feature cache
  (~35-55 clips/s with 12 workers), `py-train -m trainer.train.run --project ... --profile fast --steps 600 --job /tmp/j`
  is a ~6 min smoke run, `py-train -m trainer.train.calibrate --model x.tflite --project ...` scores any model
  (also the old trainer's `F:\Github\wake-word\models\dscharwis.tflite`). Training runs at ~35 steps/s on the 3090,
  ~12 steps/s including validation. Changing augmentation or storage needs a bump of `FEATURE_VERSION` in
  `trainer/train/sources.py`; changing the model flags in `run.py` must keep `spectrogram_length() <= CROP`.
- Mining (`trainer/mining/`): `py-train -m trainer.mining.run --project ... --run <train run id> --sources kartoffelphon --job /tmp/j`
  scans 33.8 h in ~1 min on the CPU (TFLite like the device, no TF import, so no GPU needed). Utterances are streamed
  back to back; never restart the microfrontend per clip (its noise estimate makes clip starts trigger).
  The scan job then runs `py-omni -m trainer.mining.triage` as a child (strip LD_LIBRARY_PATH between venvs). The
  check only auto-labels negatives because the source is declared wake-word free: phones and Whisper alone miss
  real wake words (measured), so never let them label without that guarantee. A person's label always wins.
- Export (`trainer/export.py`, `trainer/server/models_api.py`): exports live in `data/projects/<slug>/models/<id>/`,
  the newest is "current" and served at a stable URL for ESPHome. The manifest must keep only ESPHome's keys
  (extra keys fail its validation). The port binds to 127.0.0.1 unless `WAKEFORGE_BIND` is set in `.env`.
- Modules run with `python script.py` inside the container need `-e PYTHONPATH=/app`; `-m trainer...` works as is.
- Quick backend run on the host without Docker (Studio only, no GPU):
  `uv run --no-project --python 3.12 --with fastapi==0.142.4 --with uvicorn==0.54.0 --with soundfile==0.14.0 --with scipy==1.18.1 --with numpy==2.5.3 python -m uvicorn trainer.server.app:app --port 8790`
- Playwright MCP may only write screenshots below `C:\Users\patric` (e.g. `~/.playwright-mcp/`).
- Reference implementation (old trainer): container `mww-trainer`, volume `mww-data`,
  repo `F:\Github\wake-word` (published models in `models/`, Sample Studio in `tools/`).

