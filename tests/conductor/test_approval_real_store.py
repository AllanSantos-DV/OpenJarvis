"""The approval gate against the concrete ``ApprovalStore``, not a stand-in.

The unit tests around ``ApprovalStoreGate`` drive a fake whose ``expire_stale``
only bumps a counter and whose actions are plain objects. That proves the gate's
branching and nothing about the storage it runs on: the JSON round-trip, the
``expires_at`` string SQLite hands back, whether ``expire_stale`` really moves
rows. An approval that outlives its deadline is a security hole, so the deadline
deserves to be exercised where it is enforced.

These write to a throwaway database file, so they run wherever the rest of the
suite runs -- no Copilot CLI, no owner, no network.
"""

from __future__ import annotations

import pytest

from openjarvis.conductor.adapters import ApprovalStoreGate
from openjarvis.tools.approval_store import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    ApprovalStore,
)

ACTION = "conductor_resume"


@pytest.fixture
def store(tmp_path):
    real = ApprovalStore(db_path=str(tmp_path / "approvals.db"))
    yield real
    real.close()


def _payload(fingerprint="fp-1", session_id="s1"):
    return {"session_id": session_id, "fingerprint": fingerprint}


def _request(gate, *, fingerprint="fp-1", session_id="s1"):
    return gate.request(
        action_type=ACTION,
        description=f"responder a sessao {session_id}",
        payload=_payload(fingerprint, session_id),
        permission_key=f"{ACTION}:{session_id}",
        tier="medium",
    )


def _queue_already_expired(store, *, fingerprint="fp-1", session_id="s1"):
    """Queue a row whose deadline is already in the past."""
    return store.queue_action(
        action_type=ACTION,
        description="decisao que envelheceu",
        payload=_payload(fingerprint, session_id),
        permission_key=f"{ACTION}:{session_id}",
        tier="medium",
        ttl_hours=-1,
    )


def test_queued_request_survives_the_json_round_trip(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)

    assert _request(gate) is False

    (queued,) = store.list_pending()
    assert queued.tier == "medium"
    assert queued.payload["session_id"] == "s1"
    assert queued.payload["fingerprint"] == "fp-1"
    # The owner must be able to decide without voice. The escape hatch has to
    # survive being written to and read back from SQLite, not merely be set on
    # the dict before the insert.
    assert "list_pending" in queued.payload["text_fallback"]


def test_approval_authorises_exactly_once(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate) is False

    (queued,) = store.list_pending()
    store.update_status(queued.id, STATUS_APPROVED)

    assert _request(gate) is True
    assert store.get_action(queued.id).status == STATUS_EXECUTED

    # Replay: the same turn asking again must not ride the spent approval.
    assert _request(gate) is False
    still_pending = store.list_pending()
    assert len(still_pending) == 1
    assert still_pending[0].id != queued.id


def test_approval_past_its_deadline_does_not_authorise(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    aged = _queue_already_expired(store)
    store.update_status(aged.id, STATUS_APPROVED)

    # `list_approved` does not filter by expiry, so without the gate's own
    # deadline check this stale decision would greenlight a fresh turn.
    assert [row.id for row in store.list_approved()] == [aged.id]

    assert _request(gate) is False
    assert store.get_action(aged.id).status == STATUS_APPROVED


def test_approval_for_another_turn_does_not_authorise(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate, fingerprint="fp-1") is False

    (queued,) = store.list_pending()
    store.update_status(queued.id, STATUS_APPROVED)

    # Same session, new content: approving one turn is not approving the next.
    assert _request(gate, fingerprint="fp-2") is False
    assert store.get_action(queued.id).status == STATUS_APPROVED


def test_expire_stale_moves_past_deadline_rows(store):
    aged = _queue_already_expired(store)
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate, session_id="s2") is False

    assert store.expire_stale() == 1
    assert store.get_action(aged.id).status == STATUS_EXPIRED
    assert [row.payload["session_id"] for row in store.list_pending()] == ["s2"]
