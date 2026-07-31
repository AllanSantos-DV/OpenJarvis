"""Real contention: many conductors racing for the same turn.

The other suites use a fake clock and sequential calls. These tests spawn actual
threads with independent store connections, which is the shape of the real
failure we care about: two conductor processes ticking at the same instant and
both deciding to answer the same session.
"""

from __future__ import annotations

import threading

from openjarvis.conductor.models import ObservationFingerprint, RetryPolicy
from openjarvis.conductor.runtime_lock import FileRuntimeLock
from openjarvis.conductor.state import SqliteClaimStore


def _fp() -> ObservationFingerprint:
    return ObservationFingerprint.from_turn(
        "sess-race", 1, "2026-07-30T20:00:00Z", "faz isso"
    )


def test_only_one_racer_wins_the_claim(tmp_path):
    """Eight conductors, one turn: exactly one may act on it."""
    db = tmp_path / "claims.db"
    fingerprint = _fp()
    winners: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def racer(index: int) -> None:
        store = SqliteClaimStore(db, policy=RetryPolicy(lease_seconds=600.0))
        try:
            start.wait(timeout=10)
            claim = store.acquire(fingerprint, f"owner-{index}")
            if claim is not None:
                with lock:
                    winners.append(claim.owner)
        finally:
            store.close()

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(winners) == 1, f"expected a single winner, got {winners}"


def test_attempts_are_counted_once_per_winner(tmp_path):
    """A losing racer must not burn an attempt on the turn."""
    db = tmp_path / "claims.db"
    fingerprint = _fp()
    start = threading.Barrier(6)

    def racer(index: int) -> None:
        store = SqliteClaimStore(db, policy=RetryPolicy(lease_seconds=600.0))
        try:
            start.wait(timeout=10)
            store.acquire(fingerprint, f"owner-{index}")
        finally:
            store.close()

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    store = SqliteClaimStore(db)
    try:
        assert store.get(fingerprint.key).attempts == 1
    finally:
        store.close()


def test_only_one_conductor_holds_the_runtime_lock(tmp_path):
    """Same race, one level up: only one process may run the conductor at all."""
    path = tmp_path / "conductor.lock"
    holders: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(8)
    acquired: list[FileRuntimeLock] = []

    def racer(index: int) -> None:
        guard = FileRuntimeLock(path)
        start.wait(timeout=10)
        if guard.acquire():
            with lock:
                holders.append(index)
                acquired.append(guard)

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert len(holders) == 1, f"expected a single holder, got {holders}"
    finally:
        for guard in acquired:
            guard.release()
