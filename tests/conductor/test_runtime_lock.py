"""Runtime singleton guard: only one conductor may run at a time."""

from __future__ import annotations

import json
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
    # Ownership transferred: the file now names THIS process.
    assert lock._read_owner()[0] == os.getpid()


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


def test_a_recycled_pid_does_not_hold_the_lock_forever(tmp_path, monkeypatch):
    """The pid in a stale lock can belong to an unrelated process later.

    The OS recycles pid numbers. A conductor that crashed leaves its number
    behind, and something else eventually gets it -- at which point a pid-only
    check reads "still running" and orchestration is dead on this machine until
    a human deletes a file. Not hypothetical: a stale pid from this project was
    found already reassigned.
    """
    from openjarvis.conductor import runtime_lock as module

    path = tmp_path / "conductor.lock"
    path.write_text(
        json.dumps({"pid": 4242, "started": "quando o conductor rodou"}),
        encoding="utf-8",
    )

    # The number is alive again -- as a different process.
    monkeypatch.setattr(module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(module, "_start_time", lambda pid: "outro processo, depois")

    assert FileRuntimeLock(path).acquire() is True


def test_the_real_owner_still_holds_the_lock(tmp_path, monkeypatch):
    from openjarvis.conductor import runtime_lock as module

    path = tmp_path / "conductor.lock"
    path.write_text(
        json.dumps({"pid": 4242, "started": "o mesmo instante"}), encoding="utf-8"
    )

    monkeypatch.setattr(module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(module, "_start_time", lambda pid: "o mesmo instante")

    assert FileRuntimeLock(path).acquire() is False


def test_an_unknown_start_time_is_respected(tmp_path, monkeypatch):
    """Unknown identity must not free a live lock.

    Being wrong this way costs one skipped tick. Being wrong the other way runs
    two conductors and answers the same session twice.
    """
    from openjarvis.conductor import runtime_lock as module

    path = tmp_path / "conductor.lock"
    path.write_text("4242", encoding="utf-8")  # old format: bare pid

    monkeypatch.setattr(module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(module, "_start_time", lambda pid: "")

    assert FileRuntimeLock(path).acquire() is False


def test_an_old_bare_pid_lock_is_still_readable(tmp_path, monkeypatch):
    from openjarvis.conductor import runtime_lock as module

    path = tmp_path / "conductor.lock"
    path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(module, "_pid_alive", lambda pid: False)

    assert FileRuntimeLock(path).acquire() is True
