"""Long-running pipeline jobs as detached subprocesses (one GPU job at a time).

  <project>/jobs/<YYYYMMDD-HHMMSS>-<kind>/job.json       kind, command, pid, started
  <project>/jobs/<YYYYMMDD-HHMMSS>-<kind>/progress.json  written by the worker (state, done, total, ...)
  <project>/jobs/<YYYYMMDD-HHMMSS>-<kind>/log.txt        worker stdout + stderr
  data/system/jobs/...                                     the same for jobs of no project (the setup)

Jobs run in their own session, so uvicorn reloads neither kill them nor lose track of them:
the state is derived from the files and the pid alone.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from trainer.paths import PROJECTS, REPO_ROOT, SYSTEM

JOB_RE = re.compile(r"^\d{8}-\d{6}-[a-z]+$")
LOG_TAIL_BYTES = 6000
LOG_NOISE = ("W:onnxruntime", "XNNPACK delegate")
REPLACE_TRIES = 50  # x 0.1 s


def write_progress_file(job: Path, data: dict) -> None:
    """Atomically replace <job>/progress.json (workers in every venv use this).

    On the Windows bind mount the rename fails with EACCES while a Windows process has the file open (an editor, a
    script polling it from the host), which once killed a training run at step 10000. A progress update must not end
    a job, so the rename is retried for a few seconds.
    """
    tmp = job / "progress.json.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, job / "progress.json")
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.1)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A zombie child still answers kill(0); reap it if it is ours.
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def job_alive(job_dir: Path, job: dict) -> bool:
    """The pid must be alive AND belong to this job: pids restart at 1 when the container is recreated."""
    pid = job.get("pid", -1)
    if not pid_alive(pid):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return True  # no /proc (host run): trust the pid
    return str(job_dir) in cmdline


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def log_tail(path: Path) -> str:
    if not path.is_file():
        return ""
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - LOG_TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return "\n".join(line for line in lines if not any(n in line for n in LOG_NOISE))[-LOG_TAIL_BYTES:]


def timing(job: dict, progress: dict, alive: bool) -> dict:
    """Run time so far (or in total) and seconds since the worker's last progress write, on the container's clock."""
    now, updated = time.time(), progress.get("updated")
    try:
        started = datetime.fromisoformat(job["started"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return {"elapsed": None, "age": None}
    end = now if alive else updated or now
    return {"elapsed": round(max(0.0, end - started)), "age": round(now - updated) if alive and updated else None}


def info(job_dir: Path, with_log: bool = False) -> dict:
    job = read_json(job_dir / "job.json")
    progress = read_json(job_dir / "progress.json")
    state = progress.get("state", "starting")
    alive = job_alive(job_dir, job)
    if state in ("starting", "running") and not alive:
        state = "failed"
        progress.setdefault("message", "worker exited unexpectedly, see log")
    result = {"id": job_dir.name, "project": job_dir.parent.parent.name, "kind": job.get("kind"), "started": job.get("started"),
              "params": job.get("params", {}), **progress, "state": state, "alive": alive, **timing(job, progress, alive)}
    if with_log:
        result["log"] = log_tail(job_dir / "log.txt")
    return result


def job_dirs(project: Path | None = None) -> list[Path]:
    if project:
        roots = [project / "jobs"]
    else:
        roots = ([p / "jobs" for p in PROJECTS.iterdir()] if PROJECTS.is_dir() else []) + [SYSTEM / "jobs"]
    return sorted((d for r in roots if r.is_dir() for d in r.iterdir() if JOB_RE.match(d.name)), key=lambda d: d.name)


def active() -> dict | None:
    for d in reversed(job_dirs()):
        if job_alive(d, read_json(d / "job.json")):
            return info(d)
    return None


def latest(project: Path, kind: str) -> dict | None:
    for d in reversed(job_dirs(project)):
        if d.name.endswith(f"-{kind}"):
            return info(d, with_log=True)
    return None


def start(project: Path, kind: str, command: list[str], params: dict) -> dict:
    running = active()
    if running:
        raise ValueError(f"another job is running: {running['project']} / {running['kind']}")
    job_dir = project / "jobs" / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}"
    job_dir.mkdir(parents=True)
    command = [*command, "--job", str(job_dir)]
    with open(job_dir / "log.txt", "wb") as log:
        env = {**os.environ, "TQDM_DISABLE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1", "PYTHONWARNINGS": "ignore"}
        proc = subprocess.Popen(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env)
    job = {"kind": kind, "command": command, "params": params, "pid": proc.pid,
           "started": datetime.now().isoformat(timespec="seconds")}
    (job_dir / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    return info(job_dir)


def job_dir(project: Path, job_id: str) -> Path:
    if not JOB_RE.match(job_id or ""):
        raise ValueError(f"invalid job id: {job_id!r}")
    path = project / "jobs" / job_id
    if not (path / "job.json").is_file():
        raise FileNotFoundError(f"job not found: {job_id}")
    return path


def stop(path: Path) -> dict:
    """Ask the worker to stop after its current step (SIGTERM); it flushes its output itself."""
    job = read_json(path / "job.json")
    pid = job.get("pid", -1)
    if job_alive(path, job):
        os.killpg(pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
    return info(path, with_log=True)
