"""One app at a time, started in its own process group, with a log ring buffer.

Why one at a time: every app here takes over the same physical robot — the mic,
the camera, the motors, the speaker. Two of them at once is not "slow", it is
two processes fighting over one WebRTC media session. The manager refuses a
second launch instead of letting the robot decide.

Why a process *group*: run.sh ends in `exec python -m app.main | tee log`, so
the thing we spawn is a shell with children. Killing the shell alone leaves the
python (and the robot session it holds) alive. `start_new_session=True` puts the
whole tree in a fresh group and we signal the group.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG_LINES = 200


class LaunchError(RuntimeError):
    pass


@dataclass
class Run:
    app_id: str
    mode: str            # "robot" | "dev"
    label: str
    cmd: tuple[str, ...]
    cwd: Path
    pid: int
    started_at: float
    proc: subprocess.Popen
    lines: deque
    lock: threading.Lock
    total_seen: int = 0   # lines ever produced, incl. ones the ring dropped

    def snapshot(self) -> dict[str, Any]:
        rc = self.proc.poll()
        return {
            "app_id": self.app_id,
            "mode": self.mode,
            "label": self.label,
            "cmd": " ".join(shlex.quote(c) for c in self.cmd),
            "cwd": str(self.cwd),
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime": time.time() - self.started_at,
            "running": rc is None,
            "returncode": rc,
        }


class ProcessManager:
    """Owns at most one running app."""

    def __init__(self, log_lines: int = LOG_LINES) -> None:
        self._log_lines = log_lines
        self._run: Run | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- queries

    @property
    def current(self) -> Run | None:
        """The current run, alive or just-finished (kept so its logs survive)."""
        return self._run

    def is_running(self) -> bool:
        r = self._run
        return r is not None and r.proc.poll() is None

    def status(self) -> dict[str, Any]:
        r = self._run
        if r is None:
            return {"running": False, "run": None}
        snap = r.snapshot()
        return {"running": snap["running"], "run": snap}

    def logs(self, since: int = 0) -> dict[str, Any]:
        r = self._run
        if r is None:
            return {"lines": [], "total": 0, "app_id": None}
        with r.lock:
            all_lines = list(r.lines)
            total = r.total_seen
        # `since` counts lines ever produced, so a client that polls never
        # re-renders what it already has, even after the ring buffer rotates.
        first_kept = total - len(all_lines)
        start = max(0, since - first_kept)
        return {
            "app_id": r.app_id,
            "lines": all_lines[start:],
            "total": total,
            "running": r.proc.poll() is None,
        }

    # ------------------------------------------------------------- actions

    def launch(
        self,
        *,
        app_id: str,
        mode: str,
        label: str,
        cmd: list[str] | tuple[str, ...],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                cur = self._run.app_id  # type: ignore[union-attr]
                raise LaunchError(
                    f"{cur} is already running — stop it first "
                    f"(one app at a time: they share the mic, camera and motors)"
                )
            cwd = Path(cwd)
            if not cwd.is_dir():
                raise LaunchError(f"repo not found: {cwd}")

            argv = list(cmd)
            head = argv[0]
            if head.startswith("./") or head.startswith("../"):
                script = (cwd / head).resolve()
                if not script.exists():
                    raise LaunchError(f"launch script not found: {script}")
                if not os.access(script, os.X_OK):
                    raise LaunchError(f"launch script is not executable: {script}")

            full_env = dict(os.environ)
            if env:
                full_env.update(env)
            # Unbuffered python inside the app, so its log lines reach us live.
            full_env.setdefault("PYTHONUNBUFFERED", "1")

            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=full_env,
                    text=True,
                    bufsize=1,
                    start_new_session=True,  # own process group → group kill works
                )
            except OSError as exc:
                raise LaunchError(f"could not start {' '.join(argv)}: {exc}") from exc

            run = Run(
                app_id=app_id,
                mode=mode,
                label=label,
                cmd=tuple(argv),
                cwd=cwd,
                pid=proc.pid,
                started_at=time.time(),
                proc=proc,
                lines=deque(maxlen=self._log_lines),
                lock=threading.Lock(),
            )
            self._run = run

            t = threading.Thread(target=self._pump, args=(run,), daemon=True)
            t.start()
            return run.snapshot()

    def _pump(self, run: Run) -> None:
        assert run.proc.stdout is not None
        try:
            for line in run.proc.stdout:
                with run.lock:
                    run.lines.append(line.rstrip("\n"))
                    run.total_seen += 1
        except (ValueError, OSError):
            pass
        finally:
            try:
                run.proc.stdout.close()
            except Exception:
                pass
            run.proc.wait()
            with run.lock:
                rc = run.proc.returncode
                run.lines.append(f"— process exited with code {rc} —")
                run.total_seen += 1

    def stop(self, timeout_int: float = 5.0, timeout_term: float = 3.0) -> dict[str, Any]:
        """SIGINT (these apps say goodbye and re-centre on it), then SIGTERM,
        then SIGKILL — always to the whole process group."""
        run = self._run
        if run is None or run.proc.poll() is not None:
            return {"stopped": False, "reason": "nothing running"}

        pgid = os.getpgid(run.pid)
        sent = []
        for sig, timeout in ((signal.SIGINT, timeout_int), (signal.SIGTERM, timeout_term)):
            try:
                os.killpg(pgid, sig)
                sent.append(sig.name)
            except ProcessLookupError:
                break
            try:
                run.proc.wait(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
                sent.append("SIGKILL")
                run.proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

        # Anything still alive in the group (children that ignored the signal).
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        return {
            "stopped": True,
            "app_id": run.app_id,
            "signals": sent,
            "returncode": run.proc.returncode,
        }
