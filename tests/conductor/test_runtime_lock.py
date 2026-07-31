"""Runtime singleton guard: only one conductor may run at a time."""

from __future__ import annotations

import os

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


def test_release_lets_the_next_process_in(tmp_path):
    path = tmp_path / "conductor.lock"
    first = FileRuntimeLock(path)
    second = FileRuntimeLock(path)
    first.acquire()

    first.release()

    assert second.acquire() is True


def test_acquire_is_idempotent_for_the_holder(tmp_path):
    lock = FileRuntimeLock(tmp_path / "conductor.lock")
    assert lock.acquire() is True
    assert lock.acquire() is True


def test_release_without_holding_is_safe(tmp_path):
    FileRuntimeLock(tmp_path / "conductor.lock").release()


def test_stale_lock_from_a_dead_process_is_reclaimed(tmp_path, monkeypatch):
    """A crashed conductor must not block the machine forever."""
    path = tmp_path / "conductor.lock"
    path.write_text("999999", encoding="utf-8")

    monkeypatch.setattr(
        "openjarvis.conductor.runtime_lock._pid_alive", lambda pid: False
    )

    lock = FileRuntimeLock(path)
    assert lock.acquire() is True
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_lock_held_by_a_live_process_is_respected(tmp_path, monkeypatch):
    path = tmp_path / "conductor.lock"
    path.write_text("999999", encoding="utf-8")

    monkeypatch.setattr(
        "openjarvis.conductor.runtime_lock._pid_alive", lambda pid: True
    )

    assert FileRuntimeLock(path).acquire() is False


def test_context_manager_refuses_to_start_a_second_conductor(tmp_path):
    path = tmp_path / "conductor.lock"
    with FileRuntimeLock(path):
        with pytest.raises(RuntimeError, match="already holds"):
            with FileRuntimeLock(path):
                pass

    # Released on exit, so a later run can start.
    with FileRuntimeLock(path):
        pass


def test_corrupt_lock_file_is_treated_as_stale(tmp_path, monkeypatch):
    path = tmp_path / "conductor.lock"
    path.write_text("not-a-pid", encoding="utf-8")

    assert FileRuntimeLock(path).acquire() is True
