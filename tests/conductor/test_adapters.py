"""Real adapter seams that do not need the real Copilot CLI."""

from __future__ import annotations

from dataclasses import dataclass

from openjarvis.conductor.adapters import ApprovalStoreGate, CopilotCliResumeExecutor


@dataclass
class _Rule:
    decision: str


class _Action:
    def __init__(self, action_type, payload, status, id_="a1") -> None:
        self.action_type = action_type
        self.payload = payload
        self.status = status
        self.id = id_


class FakeApprovalStore:
    def __init__(self) -> None:
        self.permission = None
        self.approved = []
        self.queued = []
        self.status_updates = []
        self.expired = 0

    def expire_stale(self):
        self.expired += 1
        return self.expired

    def get_permission(self, permission_key):
        return self.permission

    def list_approved(self):
        return list(self.approved)

    def update_status(self, action_id, status):
        self.status_updates.append((action_id, status))

    def queue_action(self, **kwargs):
        self.queued.append(kwargs)
        return _Action(kwargs["action_type"], kwargs["payload"], "pending")


class _Agent:
    def __init__(self, content="ok", error=False) -> None:
        self.content = content
        self.metadata = {"error": error} if error else {}

    def run(self, prompt):
        return self


def test_resume_executor_passes_cwd_as_copilot_workspace():
    seen = {}

    def factory(session_id, *, workspace=""):
        seen["session_id"] = session_id
        seen["workspace"] = workspace
        return _Agent()

    result = CopilotCliResumeExecutor(agent_factory=factory).resume(
        "s1", "ping", cwd=r"C:\repo\target"
    )

    assert result.ok is True
    assert seen == {"session_id": "s1", "workspace": r"C:\repo\target"}


def test_approval_gate_exposes_text_fallback_in_queued_payload():
    store = FakeApprovalStore()
    gate = ApprovalStoreGate(store=store)

    allowed = gate.request(
        action_type="copilot_resume",
        description="Retomar sessao",
        payload={"session_id": "s1", "fingerprint": "fp1"},
        permission_key="copilot_resume:repo",
        tier="high",
    )

    assert allowed is False
    queued_payload = store.queued[0]["payload"]
    assert queued_payload["fingerprint"] == "fp1"
    assert "list_pending" in queued_payload["text_fallback"]
    assert "update_status" in queued_payload["text_fallback"]


def test_approval_gate_consumes_matching_fingerprint_once():
    store = FakeApprovalStore()
    store.approved.append(
        _Action(
            "copilot_resume", {"session_id": "s1", "fingerprint": "fp1"}, "approved"
        )
    )
    gate = ApprovalStoreGate(store=store)

    allowed = gate.request(
        action_type="copilot_resume",
        description="Retomar sessao",
        payload={"session_id": "s1", "fingerprint": "fp1"},
        permission_key="copilot_resume:repo",
        tier="high",
    )

    assert allowed is True
    assert store.status_updates == [("a1", "executed")]


def test_approval_gate_rejects_replay_for_different_fingerprint():
    store = FakeApprovalStore()
    store.approved.append(
        _Action(
            "copilot_resume", {"session_id": "s1", "fingerprint": "old"}, "approved"
        )
    )
    gate = ApprovalStoreGate(store=store)

    allowed = gate.request(
        action_type="copilot_resume",
        description="Retomar sessao",
        payload={"session_id": "s1", "fingerprint": "new"},
        permission_key="copilot_resume:repo",
        tier="high",
    )

    assert allowed is False
    assert store.status_updates == []
    assert store.queued


def test_approval_gate_honours_permission_memory():
    store = FakeApprovalStore()
    store.permission = _Rule("always_approve")
    gate = ApprovalStoreGate(store=store)

    assert gate.request(
        action_type="copilot_resume",
        description="Retomar sessao",
        payload={"session_id": "s1", "fingerprint": "fp1"},
        permission_key="copilot_resume:repo",
        tier="low",
    )
