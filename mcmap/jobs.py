"""Generation jobs: a single worker runs Arnis for one queued request at a time."""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import nbt
from .arnis import ArnisInstaller, GenerationRequest, build_command
from .config import Config
from .worlds import dir_size, is_within, safe_folder_name, unique_path

LOG_LIMIT = 600

# Arnis prints "[n/7] Doing something..." per phase; these weights are a rough share
# of total runtime per phase, so the overall bar moves at a believable pace.
STEP_WEIGHTS = (0.12, 0.03, 0.15, 0.05, 0.40, 0.15, 0.10)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_STEP_RE = re.compile(r"^\s*\[(\d)(b?)/7\]\s*(.*)$")
_COUNT_RE = re.compile(r"\]\s+(\d+)/(\d+)\b")
_WORLD_RE = re.compile(r"Created new world at:\s*(.+?)\s*$")
_BEDROCK_RE = re.compile(r"Bedrock world saved to:\s*(.+?)\s*$")
_PREVIEW_RE = re.compile(r"Map preview saved to:\s*(.+?)\s*$")
_ERROR_RE = re.compile(r"^\s*Error:\s*(.+)$")


class OutputParser:
    """Turns the raw byte stream of an Arnis run into log lines and progress."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self.step = 0
        self.step_label = "Starting Arnis…"
        self._fraction = 0.0
        self._best = 0.0
        self.world_path: str | None = None
        self.preview_path: str | None = None
        self.errors: list[str] = []

    @property
    def progress(self) -> float:
        if self.step == 0:
            return 0.0
        start = sum(STEP_WEIGHTS[: self.step - 1])
        current = start + STEP_WEIGHTS[self.step - 1] * self._fraction
        self._best = max(self._best, min(current, 0.99))
        return self._best

    def feed(self, data: bytes) -> list[str]:
        """Consume a chunk of output; return completed lines worth logging."""
        self._buffer += self._decoder.decode(data)
        *complete, self._buffer = self._buffer.split("\n")
        lines = [line for raw in complete if (line := self._finish_line(raw))]
        # Progress bars redraw in place with \r and never end the line, so peek at them.
        # The last frame may be cut off mid-number ("50/2" of "50/200"), so only trust
        # it once something other than a digit follows.
        segments = self._buffer.split("\r")
        for i, segment in enumerate(segments):
            text = _ANSI_RE.sub("", segment)
            if i < len(segments) - 1 or (text and not text[-1].isdigit()):
                self._scan_progress(text)
        return lines

    def flush(self) -> list[str]:
        self._buffer += self._decoder.decode(b"", final=True)
        raw, self._buffer = self._buffer, ""
        line = self._finish_line(raw)
        return [line] if line else []

    def _finish_line(self, raw: str) -> str | None:
        segments = [_ANSI_RE.sub("", s) for s in raw.split("\r")]
        for segment in segments:
            self._scan_progress(segment)
        line = next((s.rstrip() for s in reversed(segments) if s.strip()), "")
        if not line or _COUNT_RE.search(line):
            return None  # blank, or a progress bar frame: not worth keeping in the log
        self._scan_line(line)
        return line

    def _scan_progress(self, text: str) -> None:
        m = _COUNT_RE.search(text)
        if m and self.step:
            pos, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                self._fraction = max(self._fraction, min(pos / total, 1.0))

    def _scan_line(self, line: str) -> None:
        if m := _STEP_RE.match(line):
            step = int(m.group(1))
            if 1 <= step <= len(STEP_WEIGHTS):
                if step != self.step:
                    self.step, self._fraction = step, 0.0
                if m.group(2):  # "[6b/7]" is the second half of phase 6
                    self._fraction = max(self._fraction, 0.5)
                self.step_label = m.group(3).strip() or self.step_label
        elif m := _WORLD_RE.search(line) or _BEDROCK_RE.search(line):
            self.world_path = m.group(1)
        elif m := _PREVIEW_RE.search(line):
            self.preview_path = m.group(1)
        elif m := _ERROR_RE.match(line):
            self.errors.append(m.group(1))


@dataclass
class Job:
    id: str
    request: dict[str, Any]
    name: str
    created: float
    status: str = "queued"  # queued | running | done | failed | cancelled
    step: str = "Waiting in queue"
    progress: float = 0.0
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    world_path: str | None = None
    preview_path: str | None = None
    world_size: int | None = None
    installed: bool = False
    command: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def edition(self) -> str:
        return self.request.get("edition", "java")

    def to_dict(self, include_log: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_log:
            d.pop("log")
            d.pop("command")
        d["edition"] = self.edition
        d["world_exists"] = bool(self.world_path and Path(self.world_path).exists())
        d["has_preview"] = bool(self.preview_path and Path(self.preview_path).is_file())
        return d

    def append_log(self, lines: list[str]) -> None:
        self.log.extend(lines)
        if len(self.log) > LOG_LIMIT:
            del self.log[: len(self.log) - LOG_LIMIT]


class JobManager:
    def __init__(self, config: Config, installer: ArnisInstaller):
        self.config = config
        self.installer = installer
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._proc: subprocess.Popen[bytes] | None = None
        self._cancelled: set[str] = set()
        self._last_save = 0.0
        self._load()
        threading.Thread(target=self._worker, name="mcmap-worker", daemon=True).start()

    # ---- queries -------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in reversed(self._jobs.values())]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str, include_log: bool = True) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict(include_log=include_log) if job else None

    # ---- actions -------------------------------------------------------------

    def submit(self, req: GenerationRequest) -> dict[str, Any]:
        name = req.name or time.strftime("World %Y-%m-%d %H.%M")
        job = Job(id=uuid.uuid4().hex[:10], request=req.to_dict(), name=name, created=time.time())
        with self._lock:
            self._jobs[job.id] = job
            self._save(force=True)
        self._queue.put(job.id)
        return job.to_dict()

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self._require(job_id)
        req = GenerationRequest.from_dict(job.request)
        req.name = job.name
        return self.submit(req)

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._require(job_id)
            if job.status == "queued":
                self._finish(job, "cancelled", "Cancelled")
                return
            if job.status != "running":
                return
            self._cancelled.add(job_id)
            job.step = "Cancelling…"
            proc = self._proc
        if proc and proc.poll() is None:
            _terminate(proc)

    def install(self, job_id: str) -> dict[str, Any]:
        """Move a finished Java world into the Minecraft saves folder."""
        with self._lock:
            job = self._require(job_id)
            if job.status != "done" or job.edition != "java":
                raise ValueError("Only finished Java worlds can be added to Minecraft")
            if job.installed:
                return job.to_dict()
            if not self.config.saves_dir:
                raise ValueError("No Minecraft saves folder found. Start MCMap with --saves-dir.")
            if not job.world_path or not Path(job.world_path).is_dir():
                raise ValueError("The world folder no longer exists")
            self._move_to_saves(job)
            self._save(force=True)
            return job.to_dict()

    def remove(self, job_id: str, delete_files: bool) -> None:
        with self._lock:
            job = self._require(job_id)
            if job.status in ("queued", "running"):
                raise ValueError("Cancel the job before removing it")
            del self._jobs[job_id]
            self._save(force=True)
        if delete_files:
            # Only ever delete our own scratch folder, never a world in Minecraft's saves.
            job_dir = self.config.worlds_dir / job.id
            if job_dir.exists() and is_within(job_dir, self.config.worlds_dir):
                shutil.rmtree(job_dir, ignore_errors=True)

    def shutdown(self) -> None:
        """Stop a running Arnis process; it runs in its own session, so Ctrl+C misses it."""
        with self._lock:
            proc = self._proc
            for job in self._jobs.values():
                if job.status == "running":
                    self._cancelled.add(job.id)
        if proc and proc.poll() is None:
            _terminate(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    # ---- worker --------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if not job or job.status != "queued":
                    continue
                job.status, job.started, job.step = "running", time.time(), "Starting…"
                self._save(force=True)
            try:
                self._run(job)
            except Exception as exc:
                with self._lock:
                    job.append_log([f"MCMap error: {exc}"])
                    self._finish(job, "failed", "Failed", error=str(exc))

    def _run(self, job: Job) -> None:
        req = GenerationRequest.from_dict(job.request)
        if not self.installer.binary_path():
            with self._lock:
                job.step = "Downloading Arnis engine…"
        binary = self.installer.ensure_installed()
        with self._lock:
            if job.id in self._cancelled:
                self._cancelled.discard(job.id)
                self._finish(job, "cancelled", "Cancelled")
                return

        out_dir = self.config.worlds_dir / job.id
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        cmd = build_command(binary, req, out_dir)
        with self._lock:
            job.command = cmd
            job.append_log(["$ " + " ".join(cmd)])

        rc, parser = self._execute(job, cmd)

        with self._lock:
            if job.id in self._cancelled:
                self._cancelled.discard(job.id)
                shutil.rmtree(out_dir, ignore_errors=True)
                self._finish(job, "cancelled", "Cancelled")
                return
            if rc != 0:
                error = parser.errors[-1] if parser.errors else f"Arnis exited with code {rc}"
                self._finish(job, "failed", "Failed", error=error)
                return

        world = self._locate_world(req, out_dir, parser)
        if not world:
            raise RuntimeError("Arnis finished but no world was found in its output folder")
        preview = self._locate_preview(req, world, out_dir, parser)
        world, preview = self._apply_name(job, req, world, preview)
        size = dir_size(world)

        with self._lock:
            job.world_path = str(world)
            job.preview_path = str(preview) if preview else None
            job.world_size = size
            if req.install and req.edition == "java" and self.config.saves_dir:
                job.step = "Adding to Minecraft…"
                self._move_to_saves(job)
            self._finish(job, "done", "Ready to play")

    def _execute(self, job: Job, cmd: list[str]) -> tuple[int, OutputParser]:
        parser = OutputParser()
        env = dict(os.environ, NO_COLOR="1")
        env.setdefault("TERM", "xterm-256color")
        cwd = self.config.cache_dir
        cwd.mkdir(parents=True, exist_ok=True)

        master = None
        if os.name == "posix":
            # A pseudo-terminal makes Arnis draw its progress bars, which carry the
            # element counts we use for fine-grained progress.
            import fcntl
            import pty
            import termios

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                    cwd=cwd, env=env, start_new_session=True,
                )
            except OSError:
                os.close(master)
                raise
            finally:
                os.close(slave)

            def read() -> bytes:
                return os.read(master, 65536)
        else:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )

            def read() -> bytes:
                assert proc.stdout is not None
                return proc.stdout.read(65536)

        with self._lock:
            self._proc = proc
            if job.id in self._cancelled:  # cancel arrived while we were starting up
                _terminate(proc)
        try:
            while True:
                try:
                    data = read()
                except OSError:  # Linux raises EIO on the pty once the child exits
                    break
                if not data:
                    break
                lines = parser.feed(data)
                with self._lock:
                    job.append_log(lines)
                    if job.id not in self._cancelled:
                        job.progress = parser.progress
                        job.step = parser.step_label
                    self._save()
        finally:
            rc = proc.wait()
            if master is not None:
                os.close(master)
            with self._lock:
                self._proc = None
                job.append_log(parser.flush())
        return rc, parser

    @staticmethod
    def _locate_world(req: GenerationRequest, out_dir: Path, parser: OutputParser) -> Path | None:
        if parser.world_path and Path(parser.world_path).exists():
            return Path(parser.world_path)
        if req.edition == "bedrock":
            return next(iter(sorted(out_dir.glob("*.mcworld"))), None)
        return next((p.parent for p in sorted(out_dir.glob("*/level.dat"))), None)

    @staticmethod
    def _locate_preview(req: GenerationRequest, world: Path, out_dir: Path, parser: OutputParser) -> Path | None:
        if parser.preview_path and Path(parser.preview_path).is_file():
            return Path(parser.preview_path)
        candidates = [world / "arnis_world_map.png"] if req.edition == "java" else sorted(out_dir.glob("*.png"))
        return next((p for p in candidates if p.is_file()), None)

    def _apply_name(self, job: Job, req: GenerationRequest, world: Path, preview: Path | None) -> tuple[Path, Path | None]:
        """Rename Arnis's 'Arnis World N' output to the user's world name."""
        folder = safe_folder_name(job.name)
        if req.edition == "bedrock":
            target = unique_path(world.parent, folder, ".mcworld")
            world = world.rename(target) if target != world else world
            if preview and preview.parent == world.parent:
                preview = preview.rename(unique_path(world.parent, f"{folder} map", ".png"))
            return world, preview

        target = world if world.name == folder else unique_path(world.parent, folder)
        if target != world:
            world.rename(target)
            if preview and is_within(preview, world):
                preview = target / preview.relative_to(world)
        self._set_level_name(job, target / "level.dat", job.name)
        return target, preview

    def _move_to_saves(self, job: Job) -> None:
        assert self.config.saves_dir and job.world_path
        src = Path(job.world_path)
        self.config.saves_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(self.config.saves_dir, src.name)
        shutil.move(str(src), str(dest))
        if dest.name != src.name:
            # Keep the in-game name distinct from the world it collided with.
            self._set_level_name(job, dest / "level.dat", dest.name)
        if job.preview_path and is_within(Path(job.preview_path), src):
            job.preview_path = str(dest / Path(job.preview_path).relative_to(src))
        job.world_path = str(dest)
        job.installed = True
        job.append_log([f"Added to Minecraft: {dest}"])
        try:
            (self.config.worlds_dir / job.id).rmdir()  # only succeeds when empty
        except OSError:
            pass

    @staticmethod
    def _set_level_name(job: Job, level_dat: Path, name: str) -> None:
        try:
            nbt.set_level_name(level_dat, name)
        except Exception as exc:  # cosmetic; keep the world even if this fails
            job.append_log([f"Warning: could not set world name in level.dat: {exc}"])

    def _finish(self, job: Job, status: str, step: str, error: str | None = None) -> None:
        job.status, job.step, job.error, job.finished = status, step, error, time.time()
        if status == "done":
            job.progress = 1.0
        self._save(force=True)

    # ---- persistence ---------------------------------------------------------

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 2.0:
            return
        self._last_save = now
        data = [asdict(j) for j in self._jobs.values()]
        path = self.config.jobs_file
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def _load(self) -> None:
        path = self.config.jobs_file
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        known = {f.name for f in fields(Job)}
        for item in raw:
            try:
                job = Job(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            if job.status in ("queued", "running"):
                job.status, job.step = "failed", "Interrupted"
                job.error = "MCMap was stopped before this job finished"
            self._jobs[job.id] = job


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        return

    def kill_later() -> None:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()

    threading.Thread(target=kill_later, daemon=True).start()
