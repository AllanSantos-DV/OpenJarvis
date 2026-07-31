"""Runtime singleton for the conductor, on the OS's own file lock.

Two conductors answering the same session is the failure this prevents. A check
that reads a file or a status column cannot prevent it: the read and the launch
are separate moments, so both processes can see "nobody is running" and both
start.

**The lock is the operating system's**, through :class:`FileLockSingleton`
(``msvcrt.locking`` on Windows, ``flock`` on POSIX). That choice matters more
than it looks: an OS lock is released by the KERNEL when the holder dies, so a
crashed conductor cannot leave a lock behind. There is no stale state to detect,
no liveness probe, and no way for a recycled pid to be mistaken for a live one.

An earlier version of this file reimplemented all of that by hand -- pid file,
liveness probe, start-time comparison to survive pid recycling -- while the OS
primitive already sat in this very package, unused. Every problem that machinery
existed to solve is answered by the kernel for free.

The pid still goes into the file, purely so a human can see WHO is holding it.
Nothing decides anything from it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from openjarvis.conductor.runtime import FileLockSingleton

logger = logging.getLogger(__name__)

_DEFAULT_LOCK = Path.home() / ".jarvis-conductor" / "conductor.lock"


class FileRuntimeLock:
    """Only one conductor at a time, enforced by the OS."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_LOCK
        self._lock: Optional[FileLockSingleton] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._lock is not None

    def acquire(self) -> bool:
        """Take the lock. ``False`` when another conductor holds it."""
        if self._lock is not None:
            return True

        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLockSingleton(str(self._path))
        if not lock.try_acquire():
            logger.debug("conductor lock already held: %s", self._path)
            return False

        self._lock = lock
        self._stamp()
        return True

    def release(self) -> None:
        """Release the lock if this instance holds it (idempotent)."""
        if self._lock is None:
            return
        lock, self._lock = self._lock, None
        try:
            lock.release()
        except OSError:
            # The kernel drops it when the process exits anyway, so a failed
            # release is worth knowing about but never worth crashing over.
            logger.warning("could not release %s cleanly", self._path, exc_info=True)

    def owner(self) -> Optional[int]:
        """Pid recorded beside the lock, for diagnostics only.

        Never consulted to decide anything -- the OS owns that answer. It exists
        so "who is holding this?" has a readable reply.

        Kept in a SEPARATE file: on Windows ``msvcrt.locking`` is mandatory, so
        while the lock is held the lock file cannot even be read, let alone
        written. Measured -- reading it raises PermissionError. Diagnostics must
        not compete with the primitive they describe.
        """
        try:
            payload = json.loads(self._owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return int(payload.get("pid"))
        except (TypeError, ValueError):
            return None

    @property
    def _owner_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".owner")

    def _stamp(self) -> None:
        """Record who holds the lock, in a file the lock does not occupy."""
        try:
            self._owner_path.write_text(
                json.dumps({"pid": os.getpid()}), encoding="utf-8"
            )
        except OSError:
            logger.debug("could not stamp lock owner", exc_info=True)

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
