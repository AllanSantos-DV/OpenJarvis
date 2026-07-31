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
    STATUS_DENIED,
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


# -- the negative paths -----------------------------------------------------
#
# A gate proven only on its happy path proves the wrong thing: what matters is
# that it says NO when it should. Each of these is a way an approval could
# wrongly authorise a turn, and none of them was covered.
#
# Measured while writing them: status is guarded TWICE -- `list_approved`
# filters in SQL, and the gate re-checks. Removing either alone changes
# nothing, which is the point of defence in depth and also why a single-line
# mutation looks like dead code here. Removing BOTH turns four of these red.


def test_a_denied_action_does_not_authorise(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate) is False

    (queued,) = store.list_pending()
    store.update_status(queued.id, STATUS_DENIED)

    # Asking again must queue a fresh request, never ride the denial.
    assert _request(gate) is False
    assert store.get_action(queued.id).status == STATUS_DENIED


def test_an_expired_action_does_not_authorise(store):
    """Expired is not merely 'not approved' -- it is a decision that ran out.

    `list_approved` does not filter by status age, so a row that expire_stale
    moved must not be resurrected by a later request.
    """
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    aged = _queue_already_expired(store)
    store.expire_stale()
    assert store.get_action(aged.id).status == STATUS_EXPIRED

    assert _request(gate) is False
    assert store.get_action(aged.id).status == STATUS_EXPIRED


def test_an_already_executed_action_does_not_authorise_again(store):
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate) is False
    (queued,) = store.list_pending()
    store.update_status(queued.id, STATUS_APPROVED)
    assert _request(gate) is True

    # Spent. A replay of the same turn must not find it usable.
    assert store.get_action(queued.id).status == STATUS_EXECUTED
    assert _request(gate) is False


def test_a_remembered_denial_refuses_without_queueing(store):
    """always_deny is an answer, not an absence.

    Without honouring it the gate would re-ask forever, and the owner's "never
    do this" would decay into a queue he has to keep clearing.
    """
    from openjarvis.tools.approval_store import DECISION_ALWAYS_DENY

    store.set_permission(f"{ACTION}:s1", DECISION_ALWAYS_DENY)
    gate = ApprovalStoreGate(store=store, ttl_hours=12)

    assert _request(gate) is False
    assert store.list_pending() == []


def test_a_remembered_approval_authorises_without_queueing(store):
    from openjarvis.tools.approval_store import DECISION_ALWAYS_APPROVE

    store.set_permission(f"{ACTION}:s1", DECISION_ALWAYS_APPROVE)
    gate = ApprovalStoreGate(store=store, ttl_hours=12)

    assert _request(gate) is True
    assert store.list_pending() == []


def test_an_approval_for_another_session_does_not_leak(store):
    """Scope is per session, not per action type.

    Two sessions asking for the same kind of work must not share one decision:
    approving work in one project cannot authorise the same move in another.
    """
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate, session_id="s1") is False

    (queued,) = store.list_pending()
    store.update_status(queued.id, STATUS_APPROVED)

    assert _request(gate, session_id="s2") is False
    assert store.get_action(queued.id).status == STATUS_APPROVED


def test_a_pending_action_does_not_authorise(store):
    # The plain case, stated: queued is not decided.
    gate = ApprovalStoreGate(store=store, ttl_hours=12)
    assert _request(gate) is False
    assert _request(gate) is False
    assert len(store.list_pending()) == 2
