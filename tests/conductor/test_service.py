"""The conductor tick, exercised end to end against fakes."""

from __future__ import annotations

from typing import List

import pytest

from openjarvis.conductor.models import RetryPolicy
from openjarvis.conductor.policy import (
    TIER_HIGH,
    EligibilityPolicy,
    SessionSnapshot,
    TierPolicy,
)
from openjarvis.conductor.service import (
    ConductorService,
    ResumeResult,
    SessionDetail,
    SessionTurn,
)
from openjarvis.conductor.state import SqliteClaimStore

APP = "github"


class FakeReader:
    """In-memory stand-in for the read-only session store."""

    def __init__(self, snapshots: List[SessionSnapshot], details: dict) -> None:
        self.snapshots = snapshots
        self.details = details
        self.detail_calls: List[str] = []

    def list_idle(self, idle_minutes, limit):
        return [
            s
            for s in self.snapshots
            if s.idle_minutes is not None and s.idle_minutes >= idle_minutes
        ][:limit]

    def detail(self, session_id):
        self.detail_calls.append(session_id)
        return self.details.get(session_id)


class FakeExecutor:
    """Records resume calls and, by default, appends a turn like the real CLI."""

    def __init__(self, reader: FakeReader, *, ok: bool = True, error: str = "") -> None:
        self.reader = reader
        self.ok = ok
        self.error = error
        self.calls: List[tuple] = []

    def resume(self, session_id, prompt):
        self.calls.append((session_id, prompt))
        if not self.ok:
            return ResumeResult(ok=False, error=self.error or "boom")
        current = self.reader.details[session_id]
        appended = SessionTurn(
            turn_index=current.last_turn.turn_index + 1,
            timestamp="2026-07-30T21:00:00Z",
            user_message=prompt,
            assistant_response="continuei",
        )
        self.reader.details[session_id] = SessionDetail(
            snapshot=current.snapshot,
            last_turn=appended,
            next_steps=current.next_steps,
        )
        return ResumeResult(ok=True, content="continuei")


class RecordingGate:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests: List[dict] = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        return self.allow


def _snapshot(session_id="s-app", **kw):
    base = dict(
        session_id=session_id,
        host_type=APP,
        summary="Trabalho parado",
        cwd=r"C:\repo\projeto",
        repository="owner/projeto",
        branch="main",
        turns=12,
        last_activity="2026-07-30T20:00:00Z",
        idle_minutes=45.0,
    )
    base.update(kw)
    return SessionSnapshot(**base)


def _detail(snapshot, *, turn_index=7, next_steps="revisar Y"):
    return SessionDetail(
        snapshot=snapshot,
        last_turn=SessionTurn(
            turn_index=turn_index,
            timestamp="2026-07-30T20:00:00Z",
            user_message="faz isso",
            assistant_response="feito",
        ),
        next_steps=next_steps,
    )


@pytest.fixture
def claims(tmp_path):
    store = SqliteClaimStore(
        tmp_path / "claims.db", policy=RetryPolicy(lease_seconds=600)
    )
    yield store
    store.close()


def _service(reader, claims, executor, **kw):
    kw.setdefault("eligibility", EligibilityPolicy(idle_minutes=10.0))
    kw.setdefault("tiers", TierPolicy())
    kw.setdefault("readback_seconds", 0.0)
    return ConductorService(reader=reader, claims=claims, executor=executor, **kw)


def test_answers_an_eligible_idle_session(claims):
    snap = _snapshot(turns=1)  # trivial tier -> auto
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)

    report = _service(reader, claims, executor).tick()

    assert [o.session_id for o in report.answered] == ["s-app"]
    assert executor.calls and executor.calls[0][0] == "s-app"
    assert "revisar Y" in executor.calls[0][1]


def test_never_touches_a_cli_session(claims):
    """CLI sessions carry an empty host type and are nobody's conversation."""
    snap = _snapshot(session_id="s-cli", host_type="")
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)

    report = _service(reader, claims, executor).tick()

    assert report.answered == []
    assert executor.calls == []
    assert "host_type" in report.skipped[0].detail


def test_never_answers_its_own_session(claims):
    """Otherwise the conductor would start a conversation with itself."""
    snap = _snapshot(session_id="me")
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)
    service = _service(
        reader,
        claims,
        executor,
        eligibility=EligibilityPolicy(own_session_id="me", idle_minutes=10.0),
    )

    report = service.tick()

    assert executor.calls == []
    assert report.skipped[0].detail == "own session"


def test_ignores_sessions_outside_the_allowed_roots(claims):
    snap = _snapshot(cwd=r"C:\Users\outro\segredo")
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)
    service = _service(
        reader,
        claims,
        executor,
        eligibility=EligibilityPolicy(
            idle_minutes=10.0, allowed_roots=(r"C:\repo",)
        ),
    )

    service.tick()

    assert executor.calls == []


def test_the_same_turn_is_never_answered_twice(claims):
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)
    service = _service(reader, claims, executor)

    service.tick()
    # The executor appended a turn, so put the session back to idle for tick 2.
    reader.snapshots = [_snapshot(turns=1)]
    second = service.tick()

    assert len(executor.calls) == 2  # new turn = new unit of work
    assert second.acted == 1


def test_a_repeated_tick_without_new_activity_does_nothing(claims):
    snap = _snapshot(turns=1)
    detail = _detail(snap)
    reader = FakeReader([snap], {snap.session_id: detail})

    class FrozenExecutor(FakeExecutor):
        def resume(self, session_id, prompt):
            self.calls.append((session_id, prompt))
            return ResumeResult(ok=True, content="ok")  # nothing lands

    executor = FrozenExecutor(reader)
    service = _service(reader, claims, executor)

    service.tick()
    report = service.tick()

    assert report.answered == []
    assert len(executor.calls) == 1  # backoff blocks the immediate retry


def test_missing_new_turn_is_a_failure_not_a_success(claims):
    """Exit code zero is not proof: the reply must exist in the store."""
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})

    class LyingExecutor(FakeExecutor):
        def resume(self, session_id, prompt):
            self.calls.append((session_id, prompt))
            return ResumeResult(ok=True, content="disse que fez")

    report = _service(reader, claims, LyingExecutor(reader)).tick()

    assert report.answered == []
    assert report.failed[0].detail == "no new turn recorded"


def test_session_that_moves_before_resume_is_a_conflict(claims):
    """The owner typed while we were deciding: do not talk over them."""
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)
    service = _service(reader, claims, executor)

    original_detail = reader.detail

    def moving_detail(session_id):
        result = original_detail(session_id)
        if len(reader.detail_calls) == 2:  # the re-read just before acting
            return _detail(snap, turn_index=99)
        return result

    reader.detail = moving_detail

    report = service.tick()

    assert executor.calls == []
    assert report.conflicts[0].detail == "session moved before resume"


def test_risky_session_requires_approval(claims):
    snap = _snapshot(repository="owner/infra-prod", turns=20)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)
    gate = RecordingGate(allow=False)

    report = _service(reader, claims, executor, approvals=gate).tick()

    assert executor.calls == []
    assert report.pending[0].detail == TIER_HIGH
    assert gate.requests[0]["tier"] == TIER_HIGH


def test_approved_risky_session_is_answered(claims):
    snap = _snapshot(repository="owner/infra-prod", turns=20)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader)

    gate = RecordingGate(allow=True)
    report = _service(reader, claims, executor, approvals=gate).tick()

    assert report.acted == 1
    assert executor.calls


def test_without_an_approval_gate_only_trivial_runs(claims):
    """Fail closed: no gate configured must not mean 'do whatever you want'."""
    risky = _snapshot(session_id="s-risky", turns=20)
    reader = FakeReader([risky], {risky.session_id: _detail(risky)})
    executor = FakeExecutor(reader)

    report = _service(reader, claims, executor).tick()

    assert executor.calls == []
    assert report.pending

def test_executor_failure_is_reported_and_retryable(claims):
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    executor = FakeExecutor(reader, ok=False, error="quota exceeded")

    report = _service(reader, claims, executor).tick()

    assert report.answered == []
    assert report.failed[0].detail == "quota exceeded"


def test_one_broken_session_does_not_stop_the_tick(claims):
    good = _snapshot(session_id="s-good", turns=1)
    bad = _snapshot(session_id="s-bad", turns=1)
    reader = FakeReader([bad, good], {good.session_id: _detail(good)})
    healthy_detail = reader.detail

    def exploding_detail(session_id):
        if session_id == "s-bad":
            raise RuntimeError("db corrupted")
        return healthy_detail(session_id)

    reader.detail = exploding_detail
    executor = FakeExecutor(reader)

    report = _service(reader, claims, executor).tick()

    assert [o.session_id for o in report.failed] == ["s-bad"]
    assert [o.session_id for o in report.answered] == ["s-good"]


def test_tick_recovers_orphaned_claims_first(claims):
    """A conductor killed mid-run must not leave the session stuck."""
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})
    fingerprint = ConductorService._fingerprint(
        snap.session_id, reader.details[snap.session_id].last_turn
    )
    # Lease already expired: this is what a killed conductor leaves behind.
    claims.acquire(fingerprint, "dead-owner", lease_seconds=-1)

    report = _service(reader, claims, FakeExecutor(reader)).tick()

    assert report.recovered == 1


def test_report_summary_is_speakable(claims):
    snap = _snapshot(turns=1)
    reader = FakeReader([snap], {snap.session_id: _detail(snap)})

    report = _service(reader, claims, FakeExecutor(reader)).tick()

    assert "answered=1" in report.summary()
    assert report.acted == 1
