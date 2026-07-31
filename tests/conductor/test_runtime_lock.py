"""Runtime singleton guard: only one conductor may run at a time.

The lock is the operating system's, which changes what is worth testing. There
is no stale state to reclaim and no liveness to probe -- the kernel releases the
lock when the holder dies, so a crashed conductor cannot block the machine and a
recycled pid cannot be mistaken for a live one. Those were problems of the
hand-rolled version this replaced.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from openjarvis.conductor.runtime_lock import FileRuntimeLock


def test_second_conductor_cannot_acquire(tmp_path):
    """Two conductors would answer the same session twice."""
    path = tmp_path / "conductor.lock"
    first = FileRuntimeLock(path)
    second = FileRuntimeLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    assert first.held is True
    assert second.held is False
    first.release()


def test_release_lets_the_next_process_in(tmp_path):
    path = tmp_path / "conductor.lock"
    first = FileRuntimeLock(path)
    second = FileRuntimeLock(path)
    first.acquire()

    first.release()

    assert first.held is False
    assert second.acquire() is True
    second.release()


def test_acquire_is_idempotent_for_the_holder(tmp_path):
    path = tmp_path / "conductor.lock"
    lock = FileRuntimeLock(path)

    assert lock.acquire() is True
    assert lock.acquire() is True
    lock.release()


def test_release_without_acquire_is_harmless(tmp_path):
    FileRuntimeLock(tmp_path / "conductor.lock").release()


def test_the_lock_names_its_holder(tmp_path):
    """Diagnostics only -- nothing decides anything from this."""
    path = tmp_path / "conductor.lock"
    lock = FileRuntimeLock(path)
    lock.acquire()
    try:
        assert lock.owner() == os.getpid()
    finally:
        lock.release()


def test_context_manager_refuses_to_start_a_second_conductor(tmp_path):
    path = tmp_path / "conductor.lock"
    with FileRuntimeLock(path):
        with pytest.raises(RuntimeError, match="already holds"):
            with FileRuntimeLock(path):
                pass

    # Released on exit, so a later run can start.
    with FileRuntimeLock(path):
        pass


def test_a_dead_holder_leaves_nothing_behind(tmp_path):
    """The whole reason for using the OS lock rather than a pid file.

    A conductor that dies without releasing must not block the machine. The
    hand-rolled version needed a liveness probe and a start-time comparison to
    survive pid recycling; the kernel drops this lock when the process exits, so
    there is no stale state to reason about at all.

    Proven by killing a real process, because that is the only way to prove it.
    """
    path = tmp_path / "conductor.lock"
    script = textwrap.dedent(
        f"""
        import time
        from openjarvis.conductor.runtime_lock import FileRuntimeLock
        lock = FileRuntimeLock({str(path)!r})
        assert lock.acquire()
        print("held", flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        # While it lives, nobody else gets in.
        assert FileRuntimeLock(path).acquire() is False
    finally:
        child.kill()
        child.wait(timeout=15)

    # The kernel releases the lock as the process is torn down, which is not
    # instantaneous on Windows -- `wait` returns before the handle is gone.
    taken = FileRuntimeLock(path)
    for _ in range(40):
        if taken.acquire():
            break
        time.sleep(0.25)

    assert taken.held is True
    taken.release()
