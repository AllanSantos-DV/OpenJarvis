"""Real adapters: where the conductor's ports meet this machine.

Everything that knows about SQLite files, the Copilot CLI or the approval store
lives here, so :mod:`openjarvis.conductor.service` stays a pure decision loop.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from openjarvis.conductor.policy import SessionSnapshot
from openjarvis.conductor.service import (
    ResumeResult,
    SessionDetail,
    SessionTurn,
)

logger = logging.getLogger(__name__)


class CopilotSessionsReader:
    """Read-only adapter over the Copilot app's own session store."""

    def __init__(
        self, tool: Optional[Any] = None, store_path: Optional[str] = None
    ) -> None:
        if tool is None:
            from openjarvis.tools.copilot_sessions import CopilotSessionsTool

            tool = CopilotSessionsTool(store_path=store_path)
        self._tool = tool

    def list_idle(self, idle_minutes: float, limit: int) -> List[SessionSnapshot]:
        result = self._tool.execute(
            action="idle", idle_minutes=idle_minutes, limit=limit
        )
        if not result.success:
            logger.warning("conductor could not list sessions: %s", result.content)
            return []
        return [self._snapshot(row) for row in result.content.get("sessions", [])]

    def detail(self, session_id: str) -> Optional[SessionDetail]:
        result = self._tool.execute(action="detail", session_id=session_id)
        if not result.success:
            logger.debug("conductor could not read %s: %s", session_id, result.content)
            return None

        payload = result.content
        turns = payload.get("recent_turns") or []
        last = turns[-1] if turns else None
        checkpoint = payload.get("checkpoint") or {}

        return SessionDetail(
            snapshot=self._snapshot(payload),
            last_turn=(
                SessionTurn(
                    turn_index=int(last.get("turn_index", 0)),
                    timestamp=str(last.get("timestamp", "")),
                    user_message=str(last.get("user_message", "")),
                    assistant_response=str(last.get("assistant_response", "")),
                )
                if last
                else None
            ),
            next_steps=str(checkpoint.get("next_steps", "")),
            work_done=str(checkpoint.get("work_done", "")),
            title=str(checkpoint.get("title", "")),
        )

    @staticmethod
    def _snapshot(row: Dict[str, Any]) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=str(row.get("session_id", "")),
            host_type=str(row.get("host_type", "")),
            summary=str(row.get("summary", "")),
            cwd=str(row.get("cwd", "")),
            repository=str(row.get("repository", "")),
            branch=str(row.get("branch", "")),
            turns=int(row.get("turns", 0) or 0),
            last_activity=str(row.get("last_activity", "")),
            idle_minutes=row.get("idle_minutes"),
        )


#: Environment handed to every resumed session.
#:
#: A child ``copilot`` process inherits this machine's installed CLI plugins,
#: including the voice plugin whose agentStop hook blocks a turn that produced
#: text without calling its ``falar`` tool. That tool is provided by the desktop
#: extension and does not exist in a headless child, so the hook would block a
#: turn the session can never satisfy -- burning the timeout for nothing. Raising
#: the hook's own threshold tells it this turn is not a spoken summary, which is
#: true: the conductor speaks to the owner itself, through the notifier.
UNATTENDED_ENV = {"VOICE_SUMMARY_MIN_CHARS": "1000000"}


class CopilotCliResumeExecutor:
    """Sends a turn into an existing session through the Copilot CLI.

    The agent is built per call with the session id, and inherits the secure
    default: no tools are visible unless a caller opted into an explicit
    allowlist. Answering a session must never be a blank cheque to run commands.
    """

    def __init__(
        self,
        *,
        model: str = "auto",
        timeout: int = 300,
        per_turn_seconds: int = 10,
        max_timeout: int = 900,
        available_tools: Optional[Sequence[str]] = None,
        agent_factory: Optional[Any] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._per_turn_seconds = per_turn_seconds
        self._max_timeout = max_timeout
        self._available_tools = list(available_tools) if available_tools else None
        self._agent_factory = agent_factory
        self._env = dict(UNATTENDED_ENV)
        if env:
            self._env.update(env)

    def resume(
        self, session_id: str, prompt: str, *, cwd: str = "", turns: int = 0
    ) -> ResumeResult:
        # Resuming replays the whole conversation, so a long session costs
        # several times the tokens and the wall clock of a fresh one. A single
        # fixed timeout therefore kills exactly the sessions most worth
        # answering; scale the budget with the history instead.
        timeout = min(
            self._max_timeout,
            self._timeout + int(turns) * self._per_turn_seconds,
        )
        agent = self._build_agent(session_id, cwd=cwd, timeout=timeout)
        result = agent.run(prompt)
        if result.metadata.get("error"):
            return ResumeResult(ok=False, error=result.content)
        return ResumeResult(ok=True, content=result.content)

    def _build_agent(self, session_id: str, *, cwd: str = "", timeout: int = 0):
        if self._agent_factory is not None:
            return self._agent_factory(session_id, workspace=cwd)

        from openjarvis.agents.copilot_cli import CopilotCliAgent

        return CopilotCliAgent(
            None,
            self._model,
            temperature=0.7,
            max_tokens=1024,
            workspace=cwd,
            session_id=session_id,
            timeout=timeout or self._timeout,
            available_tools=self._available_tools,
            # The owner's session keeps the tools he granted it: it has to be
            # able to finish its own work. Safety here comes from choosing
            # which sessions to answer (eligibility + tier + approval), not
            # from disarming a session the owner already trusts.
            sandboxed=False,
            env=self._env,
        )


class ApprovalStoreGate:
    """Approval gate backed by the existing ``ApprovalStore``.

    The tick never blocks waiting for a human. A request is queued and the
    answer is ``False`` for now; a later tick sees the recorded decision (or a
    remembered ``always_approve``) and proceeds.
    """

    def __init__(
        self,
        store: Optional[Any] = None,
        *,
        db_path: Optional[str] = None,
        ttl_hours: int = 12,
    ) -> None:
        if store is None:
            from openjarvis.tools.approval_store import ApprovalStore

            store = ApprovalStore(db_path=db_path) if db_path else ApprovalStore()
        self._store = store
        self._ttl_hours = ttl_hours

    def expire_stale(self) -> int:
        return int(self._store.expire_stale())

    def request(
        self,
        *,
        action_type: str,
        description: str,
        payload: Dict[str, Any],
        permission_key: str,
        tier: str,
    ) -> bool:
        from openjarvis.tools.approval_store import (
            DECISION_ALWAYS_APPROVE,
            DECISION_ALWAYS_DENY,
            STATUS_APPROVED,
            STATUS_EXECUTED,
        )

        payload = dict(payload)
        payload.setdefault(
            "text_fallback",
            "Fallback textual: use ApprovalStore.list_pending() para ver esta "
            "pendencia e ApprovalStore.update_status(id, 'approved'|'denied') "
            "para decidir sem voz.",
        )

        remembered = self._store.get_permission(permission_key)
        if remembered is not None:
            if remembered.decision == DECISION_ALWAYS_APPROVE:
                return True
            if remembered.decision == DECISION_ALWAYS_DENY:
                return False

        # An earlier tick may already have queued this and had it approved.
        for action in self._store.list_approved():
            if (
                action.action_type == action_type
                and action.payload.get("session_id") == payload.get("session_id")
                and action.payload.get("fingerprint") == payload.get("fingerprint")
                and action.status == STATUS_APPROVED
            ):
                self._store.update_status(action.id, STATUS_EXECUTED)
                return True

        self._store.queue_action(
            action_type=action_type,
            description=description,
            payload=payload,
            permission_key=permission_key,
            tier=tier,
            ttl_hours=self._ttl_hours,
        )
        return False


class NativeJavaObservationRecorder:
    """Store successful conductor outcomes in project-scoped semantic memory."""

    def __init__(
        self,
        backend: Optional[Any] = None,
        *,
        project_id: str = "AllanSantos-DV/jarvis-home",
    ) -> None:
        if backend is None:
            from openjarvis.tools.storage.native_java import NativeJavaMemoryBackend

            backend = NativeJavaMemoryBackend(project_id=project_id)
        self._backend = backend

    def record(
        self,
        *,
        session_id: str,
        summary: str,
        tier: str,
        response: str,
        fingerprint: str,
        cwd: str = "",
        repository: str = "",
    ) -> None:
        content = (
            "Jarvis Conductor observation\n"
            f"session: {session_id}\n"
            f"summary: {summary or '(sem resumo)'}\n"
            f"tier: {tier}\n"
            f"response: {response or '(sem resposta)'}\n"
            f"fingerprint: {fingerprint}"
        )
        self._backend.store(
            content,
            source="jarvis-conductor",
            metadata={
                "kind": "conductor_observation",
                "session_id": session_id,
                "tier": tier,
                "fingerprint": fingerprint,
                "cwd": cwd,
                "repository": repository,
            },
        )


class VoiceNotifier:
    """Speaks the outcome through the local vox-engine, if it is running.

    Notification is best effort by design: the owner losing an audio ping is an
    inconvenience, while a failing notifier aborting the tick would be a bug.
    """

    def __init__(self, backend: Optional[Any] = None, voice: str = "") -> None:
        self._backend = backend
        self._voice = voice

    def notify(self, message: str) -> None:
        backend = self._backend
        if backend is None:
            from openjarvis.speech.vox_engine import VoxEngineTTSBackend

            backend = VoxEngineTTSBackend()
            self._backend = backend
        if not backend.health():
            logger.debug("vox-engine offline; skipping spoken notification")
            return
        backend.synthesize(message, voice_id=self._voice)


class LoggingNotifier:
    """Fallback notifier that only writes to the log."""

    def notify(self, message: str) -> None:
        logger.info("conductor: %s", message)


__all__ = [
    "ApprovalStoreGate",
    "CopilotCliResumeExecutor",
    "CopilotSessionsReader",
    "LoggingNotifier",
    "NativeJavaObservationRecorder",
    "VoiceNotifier",
]
