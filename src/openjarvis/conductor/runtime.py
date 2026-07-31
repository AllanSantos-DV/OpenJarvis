"""OS file-lock singleton adapter.

Single-instance is enforced by an OS-level advisory/mandatory file lock, NOT by
reading a TOML flag nor by inspecting AgentManager status. A second holder of
the same lock path cannot acquire while the first holds it.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore[assignment]

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class SingletonLockError(RuntimeError):
    """Raised when the singleton lock is already held by another holder."""


_log = logging.getLogger(__name__)


class FileLockSingleton:
    """Cross-platform exclusive file lock used as a runtime singleton."""

    def __init__(self, lock_path: str) -> None:
        self._path = lock_path
        self._fd: Optional[int] = None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        if not self._lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock(fd)
        finally:
            os.close(fd)

    @contextmanager
    def acquire(self) -> Iterator["FileLockSingleton"]:
        if not self.try_acquire():
            raise SingletonLockError(
                f"conductor singleton lock already held: {self._path}"
            )
        try:
            yield self
        finally:
            self.release()

    # --- platform primitives ---------------------------------------------
    @staticmethod
    def _lock(fd: int) -> bool:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_END)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            return True
        raise RuntimeError("no file-locking primitive available")

    @staticmethod
    def _unlock(fd: int) -> None:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                # Fail-loud, matching the POSIX path which does not swallow:
                # surface the release failure instead of hiding it.
                _log.error("failed to release singleton lock (fd=%s): %s", fd, exc)
                raise
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
