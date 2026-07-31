"""OS-level singleton guard for the conductor.

Checking a config file or a database status column cannot keep two conductors
apart: the check and the launch are separate moments, so both processes can read
"nobody is running" and then both start. Two conductors means the same session
gets answered twice.

The lock file carries the owning pid **and its start time**, because a pid alone
is not an identity: the OS recycles pids, so a lock left by a crashed conductor
can end up pointing at some unrelated process that started later. Treating that
as "still running" disables orchestration on this machine until someone deletes
a file by hand -- and it is not hypothetical, a stale pid from this project was
found already reassigned. Start time makes the pair unique in practice: the same
pid CAN come back, but not with the same creation instant.

A process-local registry complements the file, because a pid cannot distinguish
"my own crashed run left this" from "another instance inside this very process
is holding it right now".
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_LOCK = Path.home() / ".jarvis-conductor" / "conductor.lock"

#: Lock files currently held by instances inside THIS process.
_LOCAL_HOLDERS: Dict[str, int] = {}
_LOCAL_GUARD = threading.Lock()


def _start_time(pid: int) -> str:
    """A pid's creation instant, or "" when it cannot be established.

    Paired with the pid this is an identity: pids are recycled, creation
    instants are not. Empty means "unknown", and every caller treats unknown as
    "cannot prove it is a different process" rather than guessing.
    """
    if pid <= 0:
        return ""
    try:
        import subprocess

        if os.name == "nt":
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Process -Id {pid} -ErrorAction Stop).StartTime.Ticks",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return (out.stdout or "").strip()
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001 -- unknown, not an error
        return ""


def _same_process(pid: int, started: str) -> bool:
    """Whether *pid* is still the process that took the lock.

    A live pid whose start time no longer matches is a DIFFERENT process that
    inherited the number -- the lock is stale and reclaiming it is correct.
    """
    if not _pid_alive(pid):
        return False
    if not started:
        # The lock predates identity tracking, or the probe failed. Fall back to
        # the old behaviour: a live pid is respected. Being wrong here costs one
        # skipped tick; being wrong the other way answers a session twice.
        return True
    now = _start_time(pid)
    if not now:
        return True
    return now == started


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is still running (best effort, cross-platform)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - a failed probe must not free the lock
            return True
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileRuntimeLock:
    """Exclusive-create lock file carrying the owning pid."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_LOCK
        self._key = str(
            self._path.resolve() if self._path.parent.exists() else self._path
        )
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    def _write_lock(self) -> bool:
        """Create the lock file atomically. ``False`` if it already exists."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        pid = os.getpid()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": pid, "started": _start_time(pid)}))
        return True

    def _read_owner(self) -> Tuple[Optional[int], str]:
        """The recorded ``(pid, start_time)``.

        Older locks hold a bare pid; those read back with an empty start time,
        which callers treat as "identity unknown" rather than failing.
        """
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None, ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        # `json.loads("4242")` succeeds and yields an int -- the old bare-pid
        # format parses as valid JSON, just not as the object we write now.
        if not isinstance(payload, dict):
            try:
                return int(raw), ""
            except ValueError:
                return None, ""
        try:
            return int(payload.get("pid")), str(payload.get("started") or "")
        except (TypeError, ValueError):
            return None, ""

    def _read_pid(self) -> Optional[int]:
        return self._read_owner()[0]

    def acquire(self) -> bool:
        """Take the lock, reclaiming it only from an owner that is really gone."""
        if self._held:
            return True

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCAL_GUARD:
            holder = _LOCAL_HOLDERS.get(self._key)
            if holder is not None and holder != id(self):
                logger.debug(
                    "conductor lock already held in this process: %s", self._path
                )
                return False

            if self._write_lock():
                _LOCAL_HOLDERS[self._key] = id(self)
                self._held = True
                return True

            owner_pid, owner_started = self._read_owner()
            if owner_pid is not None and _same_process(owner_pid, owner_started):
                logger.debug("conductor lock held by live pid %s", owner_pid)
                return False

            # The recorded owner is gone -- either dead, or the pid was recycled
            # by an unrelated process. Reclaiming here is what keeps a crash (or
            # a coincidence in pid numbering) from permanently disabling
            # orchestration on this machine.
            logger.info("Reclaiming stale conductor lock from pid %s", owner_pid)
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            if self._write_lock():
                _LOCAL_HOLDERS[self._key] = id(self)
                self._held = True
                return True
            return False

    def release(self) -> None:
        """Release the lock if this instance holds it (idempotent)."""
        if not self._held:
            return
        with _LOCAL_GUARD:
            self._held = False
            if _LOCAL_HOLDERS.get(self._key) == id(self):
                _LOCAL_HOLDERS.pop(self._key, None)
            if self._read_pid() == os.getpid():
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass

    def __enter__(self) -> "FileRuntimeLock":
        if not self.acquire():
            raise RuntimeError(
                f"Another Jarvis conductor already holds {self._path}. "
                "Running two conductors would answer the same session twice."
            )
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


__all__ = ["FileRuntimeLock"]
