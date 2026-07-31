"""Claim store: leases, retries, crash recovery and audit."""

from __future__ import annotations

import pytest

from openjarvis.conductor.models import (
    CONFLICT,
    FAILED,
    LEASED,
    OBSERVED,
    RUNNING,
    SUCCEEDED,
    ObservationFingerprint,
    RetryPolicy,
    StaleClaimError,
)
from openjarvis.conductor.state import SqliteClaimStore


class FakeClock:
    """Injectable clock so lease and backoff behaviour is deterministic."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    s = SqliteClaimStore(
        tmp_path / "claims.db",
        policy=RetryPolicy(max_attempts=3, lease_seconds=300.0, backoff_seconds=60.0),
        clock=clock,
    )
    yield s
    s.close()


def _fp(turn_index: int = 1, content: str = "faz isso") -> ObservationFingerprint:
    return ObservationFingerprint.from_turn(
        "sess-1", turn_index, "2026-07-30T20:00:00Z", content
    )


def test_refuses_to_use_the_copilot_app_database(tmp_path):
    """Conductor state must never be written into the app's own schema."""
    with pytest.raises(ValueError, match="belongs"):
        SqliteClaimStore(tmp_path / "session-store.db")


def test_observe_is_idempotent(store):
    fp = _fp()
    first = store.observe(fp)
    second = store.observe(fp)

    assert first.state == OBSERVED
    assert second.state == OBSERVED
    assert second.attempts == 0


def test_acquire_gives_a_lease_and_counts_the_attempt(store, clock):
    claim = store.acquire(_fp(), "owner-a")

    assert claim is not None
    assert claim.state == LEASED
    assert claim.owner == "owner-a"
    assert claim.claim_token
    assert claim.attempts == 1
    assert claim.lease_expires_at == clock.now + 300.0


def test_second_owner_cannot_steal_an_active_lease(store):
    """Two ticks racing on the same turn must not both answer it."""
    fp = _fp()
    assert store.acquire(fp, "owner-a") is not None

    assert store.acquire(fp, "owner-b") is None


def test_expired_lease_can_be_taken_over(store, clock):
    fp = _fp()
    store.acquire(fp, "owner-a")

    clock.advance(301.0)

    second = store.acquire(fp, "owner-b")
    assert second is not None
    assert second.owner == "owner-b"
    assert second.attempts == 2


def test_running_requires_the_owning_token(store):
    fp = _fp()
    claim = store.acquire(fp, "owner-a")

    running = store.mark_running(claim.key, claim.claim_token)
    assert running.state == RUNNING

    with pytest.raises(StaleClaimError):
        store.mark_running(claim.key, "not-my-token")


def test_worker_that_lost_its_lease_cannot_finish_the_new_owners_work(store, clock):
    """A late worker must not record a reply the new owner is responsible for."""
    fp = _fp()
    first = store.acquire(fp, "owner-a")
    store.mark_running(first.key, first.claim_token)

    clock.advance(301.0)
    second = store.acquire(fp, "owner-b")
    assert second is not None

    with pytest.raises(StaleClaimError):
        store.mark_succeeded(first.key, first.claim_token)


def test_success_is_terminal_and_not_reprocessed(store):
    fp = _fp()
    claim = store.acquire(fp, "owner-a")
    store.mark_running(claim.key, claim.claim_token)
    done = store.mark_succeeded(claim.key, claim.claim_token, detail="replied")

    assert done.state == SUCCEEDED
    assert store.acquire(fp, "owner-b") is None


def test_failure_is_retryable_after_backoff(store, clock):
    """A crash or timeout must not burn the turn forever."""
    fp = _fp()
    claim = store.acquire(fp, "owner-a")
    failed = store.mark_failed(claim.key, claim.claim_token, error="timeout")

    assert failed.state == FAILED
    assert failed.last_error == "timeout"
    assert store.acquire(fp, "owner-a") is None  # still backing off

    clock.advance(61.0)
    retried = store.acquire(fp, "owner-a")
    assert retried is not None
    assert retried.attempts == 2


def test_retries_stop_at_max_attempts(store, clock):
    fp = _fp()
    for _ in range(3):
        claim = store.acquire(fp, "owner-a")
        assert claim is not None
        store.mark_failed(claim.key, claim.claim_token, error="boom")
        clock.advance(10_000.0)

    assert store.acquire(fp, "owner-a") is None
    assert store.get(fp.key).attempts == 3


def test_conflict_is_terminal_for_this_fingerprint(store):
    fp = _fp()
    claim = store.acquire(fp, "owner-a")
    conflicted = store.mark_conflict(claim.key, claim.claim_token, detail="turn moved")

    assert conflicted.state == CONFLICT
    assert store.acquire(fp, "owner-a") is None


def test_a_new_turn_is_a_new_unit_of_work(store):
    """Conflict on one turn must not block the session's next turn."""
    first = _fp(turn_index=1)
    claim = store.acquire(first, "owner-a")
    store.mark_conflict(claim.key, claim.claim_token)

    later = _fp(turn_index=2, content="proximo pedido")
    assert store.acquire(later, "owner-a") is not None


def test_edited_turn_produces_a_different_fingerprint():
    """Identity must include content, not just (session_id, turn_index)."""
    original = ObservationFingerprint.from_turn("s", 1, "t", "texto original")
    edited = ObservationFingerprint.from_turn("s", 1, "t", "texto editado")

    assert original.key != edited.key
    assert original.content_hash != edited.content_hash


def test_crash_recovery_reclaims_orphaned_leases(store, clock):
    """The conductor dies mid-run; the turn must not stay pinned forever."""
    fp = _fp()
    claim = store.acquire(fp, "owner-a")
    store.mark_running(claim.key, claim.claim_token)

    clock.advance(301.0)
    recovered = store.recover_expired()

    assert [c.key for c in recovered] == [claim.key]
    assert store.get(claim.key).state == FAILED
    assert store.get(claim.key).claim_token == ""

    clock.advance(61.0)
    assert store.acquire(fp, "owner-b") is not None


def test_recovery_leaves_live_leases_alone(store, clock):
    fp = _fp()
    claim = store.acquire(fp, "owner-a")

    clock.advance(10.0)
    assert store.recover_expired() == []
    assert store.get(claim.key).state == LEASED


def test_audit_records_the_decision_trail(store):
    fp = _fp()
    claim = store.acquire(fp, "owner-a")
    store.mark_running(claim.key, claim.claim_token)
    store.mark_succeeded(claim.key, claim.claim_token)
    store.acquire(fp, "owner-b")

    events = [e.event for e in store.audit()]
    assert "observed" in events
    assert "claimed" in events
    assert "running" in events
    assert "succeeded" in events
    assert "skipped" in events
    assert all(e.session_id == "sess-1" for e in store.audit())


def test_audit_keeps_the_correlation_key(store):
    fp = _fp()
    store.acquire(fp, "owner-a")

    assert all(e.fingerprint_key == fp.key for e in store.audit())


def test_separate_processes_share_state_through_the_file(tmp_path, clock):
    """Two conductor instances on the same DB must not both win the lease."""
    a = SqliteClaimStore(tmp_path / "claims.db", clock=clock)
    b = SqliteClaimStore(tmp_path / "claims.db", clock=clock)
    try:
        fp = _fp()
        assert a.acquire(fp, "proc-a") is not None
        assert b.acquire(fp, "proc-b") is None
    finally:
        a.close()
        b.close()
