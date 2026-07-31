"""The conductor tick: the loop that actually orchestrates other sessions.

This is the piece that turns registered adapters into a maestro. One tick:

1. reclaims leases orphaned by a previous crash;
2. lists sessions and drops everything the safety policy rejects;
3. builds a fingerprint of the exact turn it intends to answer;
4. claims that fingerprint, so no second conductor answers it too;
5. asks the approval gate whether it may act, given the risk tier;
6. re-reads the session and aborts if it moved while we were deciding;
7. executes the reply, then verifies a new turn really landed;
8. records success, failure or conflict, always releasing the lease.

Every step goes through a port, so the whole loop runs against fakes in tests.
Nothing here imports an agent, an engine or a model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from openjarvis.conductor.models import (
    Claim,
    ObservationFingerprint,
    StaleClaimError,
    make_owner_id,
)
from openjarvis.conductor.policy import (
    TIER_TRIVIAL,
    EligibilityPolicy,
    SessionSnapshot,
    TierPolicy,
)
from openjarvis.conductor.promotion import (
    PROMOTION_PROMPT,
    PromotionPolicy,
    confirm_promotion,
    is_autonomous,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """The last turn of a session -- what we would be replying to."""

    turn_index: int
    timestamp: str
    user_message: str = ""
    assistant_response: str = ""

    @property
    def content(self) -> str:
        return f"{self.user_message}\n{self.assistant_response}"


@dataclass(frozen=True, slots=True)
class SessionDetail:
    """Everything the conductor needs to decide and to report."""

    snapshot: SessionSnapshot
    last_turn: Optional[SessionTurn] = None
    #: Tail of the conversation, oldest first. Used to tell a session that
    #: stalled asking questions from one that simply finished its work.
    recent_turns: tuple = ()
    next_steps: str = ""
    work_done: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """Outcome of asking the target session to continue."""

    ok: bool
    content: str = ""
    error: str = ""


class SessionReader(Protocol):
    """Read-only view over the Copilot app's sessions."""

    def list_idle(self, idle_minutes: float, limit: int) -> List[SessionSnapshot]: ...

    def detail(self, session_id: str) -> Optional[SessionDetail]: ...


class ResumeExecutor(Protocol):
    """Sends one new turn into an existing session."""

    def resume(
        self, session_id: str, prompt: str, *, cwd: str = "", turns: int = 0
    ) -> ResumeResult: ...


class ApprovalGate(Protocol):
    """Decides whether an action of a given tier may run now."""

    def request(
        self,
        *,
        action_type: str,
        description: str,
        payload: Dict[str, Any],
        permission_key: str,
        tier: str,
    ) -> bool:
        """``True`` to act now; ``False`` to leave it pending for a human."""

    def expire_stale(self) -> int:
        """Expire old pending approvals before queuing or consuming decisions."""


class ObservationRecorder(Protocol):
    """Persists the outcome of a successful conductor action to semantic memory."""

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
    ) -> None: ...


class Notifier(Protocol):
    """Tells the owner what happened, out of band."""

    def notify(self, message: str) -> None: ...


@dataclass(slots=True)
class TickOutcome:
    """What the conductor did to one session during a tick."""

    session_id: str
    action: str
    detail: str = ""


@dataclass(slots=True)
class TickReport:
    """Aggregate result of one tick, suitable for logs and for speaking aloud."""

    recovered: int = 0
    considered: int = 0
    skipped: List[TickOutcome] = field(default_factory=list)
    pending: List[TickOutcome] = field(default_factory=list)
    answered: List[TickOutcome] = field(default_factory=list)
    failed: List[TickOutcome] = field(default_factory=list)
    conflicts: List[TickOutcome] = field(default_factory=list)

    @property
    def acted(self) -> int:
        return len(self.answered)

    def summary(self) -> str:
        return (
            f"considered={self.considered} answered={len(self.answered)} "
            f"pending={len(self.pending)} failed={len(self.failed)} "
            f"conflicts={len(self.conflicts)} skipped={len(self.skipped)} "
            f"recovered={self.recovered}"
        )


#: The conductor asks for a bounded status report, not for unattended work.
#:
#: This is deliberate. The session keeps its own tools, so instructing
#: the session to "carry on and do the work" would ask for something it cannot
#: perform, and the turn would burn its whole timeout achieving nothing. What
#: the owner actually needs from a stalled session is the same thing he would
#: read himself: where it stopped, what it needs, and whether it can proceed.
#: Handing real autonomy back is a separate, explicit step.
DEFAULT_PROMPT = (
    "O Jarvis esta retomando esta sessao em nome do dono, que nao esta olhando "
    "agora. Continue de onde parou, seguindo o plano ja combinado NESTA conversa. "
    "Se houver uma decisao que so o dono pode tomar, NAO adivinhe: pare e responda "
    "com a pergunta objetiva. Se nao houver trabalho pendente, responda apenas: "
    "NADA PENDENTE. {next_steps}"
)

#: Rendered in place of the checkpoint when the app never wrote one. Saying
#: "no checkpoint" out loud beats an empty line: without it the session replies
#: that no task was specified -- true of the prompt, useless to the owner, since
#: the history is right there in the session being resumed.
NO_CHECKPOINT_HINT = (
    "Nao ha checkpoint registrado: baseie-se no que voce ja fez nesta sessao."
)


class ConductorService:
    """Composes the ports into the tick described in the module docstring."""

    def __init__(
        self,
        *,
        reader: SessionReader,
        claims: Any,
        executor: ResumeExecutor,
        eligibility: Optional[EligibilityPolicy] = None,
        tiers: Optional[TierPolicy] = None,
        approvals: Optional[ApprovalGate] = None,
        notifier: Optional[Notifier] = None,
        observation_recorder: Optional[ObservationRecorder] = None,
        owner: str = "",
        limit: int = 10,
        prompt_template: str = DEFAULT_PROMPT,
        auto_tiers: Sequence[str] = (TIER_TRIVIAL,),
        promotion: Optional[PromotionPolicy] = None,
        shadow: Optional[Any] = None,
        readback_seconds: float = 120.0,
        readback_interval: float = 1.0,
    ) -> None:
        self._reader = reader
        self._claims = claims
        self._executor = executor
        self._eligibility = eligibility or EligibilityPolicy()
        self._tiers = tiers or TierPolicy()
        self._approvals = approvals
        self._notifier = notifier
        self._observation_recorder = observation_recorder
        self._owner = owner or make_owner_id()
        self._limit = limit
        self._prompt_template = prompt_template
        self._auto_tiers = tuple(auto_tiers)
        self._promotion = promotion
        self._shadow = shadow
        self._readback_seconds = readback_seconds
        self._readback_interval = readback_interval

    @property
    def owner(self) -> str:
        return self._owner

    def tick(self) -> TickReport:
        """Run one full pass. Never raises for a single bad session."""
        report = TickReport()
        report.recovered = len(self._claims.recover_expired())
        if self._approvals is not None and hasattr(self._approvals, "expire_stale"):
            self._approvals.expire_stale()

        candidates = self._reader.list_idle(self._eligibility.idle_minutes, self._limit)
        for snapshot in candidates:
            report.considered += 1
            try:
                self._handle(snapshot, report)
            except Exception as exc:  # noqa: BLE001 - one bad session must not stop the tick
                logger.exception("conductor tick failed for %s", snapshot.session_id)
                report.failed.append(
                    TickOutcome(snapshot.session_id, "error", str(exc))
                )
        logger.info("conductor tick: %s", report.summary())
        return report

    def _handle(self, snapshot: SessionSnapshot, report: TickReport) -> None:
        verdict = self._eligibility.evaluate(snapshot)
        if not verdict.eligible:
            report.skipped.append(
                TickOutcome(snapshot.session_id, "skipped", verdict.reason)
            )
            return

        detail = self._reader.detail(snapshot.session_id)
        if detail is None or detail.last_turn is None:
            report.skipped.append(
                TickOutcome(snapshot.session_id, "skipped", "no readable turn")
            )
            return

        fingerprint = self._fingerprint(snapshot.session_id, detail.last_turn)
        claim = self._claims.acquire(fingerprint, self._owner)
        if claim is None:
            report.skipped.append(
                TickOutcome(
                    snapshot.session_id, "skipped", "already handled or claimed"
                )
            )
            return

        tier = self._tiers.classify(snapshot, next_steps=detail.next_steps)
        if not self._authorised(snapshot, detail, tier, fingerprint):
            # Leave it for a human. Releasing (instead of failing) is what lets
            # the tick that finally sees the approval act on it: a failure would
            # spend an attempt and park the work behind a backoff, so the
            # approval would look ignored.
            self._claims.release(
                claim.key, claim.claim_token, reason=f"awaiting approval ({tier})"
            )
            report.pending.append(TickOutcome(snapshot.session_id, "pending", tier))
            # A queued approval nobody hears about is a dead end: the session
            # waits forever for a decision the owner does not know he owes.
            self._notify(
                f"A sessao {snapshot.summary or snapshot.session_id} precisa "
                f"da sua decisao ({tier}) para eu retomar."
            )
            return

        self._execute(claim, snapshot, detail, tier, report)

    def _authorised(
        self,
        snapshot: SessionSnapshot,
        detail: SessionDetail,
        tier: str,
        fingerprint: ObservationFingerprint,
    ) -> bool:
        if tier in self._auto_tiers:
            return True
        if self._approvals is None:
            return False
        return self._approvals.request(
            action_type="copilot_resume",
            description=(
                f"Retomar a sessao '{snapshot.summary or snapshot.session_id}' "
                f"parada ha {snapshot.idle_minutes:.0f} min"
            ),
            payload={
                "session_id": snapshot.session_id,
                "cwd": snapshot.cwd,
                "next_steps": detail.next_steps,
                "fingerprint": fingerprint.key,
            },
            permission_key=f"copilot_resume:{snapshot.repository or snapshot.cwd}",
            tier=tier,
        )

    def _execute(
        self,
        claim: Claim,
        snapshot: SessionSnapshot,
        detail: SessionDetail,
        tier: str,
        report: TickReport,
    ) -> None:
        try:
            self._claims.mark_running(claim.key, claim.claim_token)
        except StaleClaimError as exc:
            report.skipped.append(TickOutcome(snapshot.session_id, "skipped", str(exc)))
            return

        # Re-read right before acting: the owner may have typed into the session
        # while we were classifying, and answering then would talk over them.
        fresh = self._reader.detail(snapshot.session_id)
        if fresh is None or fresh.last_turn is None:
            self._finish_conflict(claim, snapshot, report, "session vanished")
            return
        if self._fingerprint(snapshot.session_id, fresh.last_turn) != claim.fingerprint:
            self._finish_conflict(
                claim, snapshot, report, "session moved before resume"
            )
            return

        if self._promote_if_stuck(claim, snapshot, detail, report):
            return

        blocked = self._shadow_objects(snapshot, detail)
        if blocked:
            # The shadow is a BRAKE, not a report. It runs here -- after the
            # claim, before the turn is sent -- because the whole point of an
            # unattended loop is that nobody is watching what it writes into a
            # real session. Releasing (not failing) keeps the attempt: the
            # session was not answered, so the turn stays available once the
            # objection is dealt with.
            self._claims.release(claim.key, claim.claim_token, reason=blocked)
            report.pending.append(
                TickOutcome(snapshot.session_id, "pending", f"sombra: {blocked}")
            )
            self._notify(
                f"O sombra barrou uma resposta na sessao {snapshot.session_id[:8]}: "
                f"{blocked}"
            )
            return

        prompt = self._prompt_template.format(
            next_steps=(
                f"Ultimo checkpoint registrado: {detail.next_steps}"
                if detail.next_steps.strip()
                else NO_CHECKPOINT_HINT
            )
        )
        result = self._executor.resume(
            snapshot.session_id, prompt, cwd=snapshot.cwd, turns=snapshot.turns
        )

        if not result.ok:
            landed_after_error = self._await_new_turn(
                snapshot.session_id, claim.fingerprint.turn_index
            )
            if landed_after_error is not None:
                self._complete_success(
                    claim,
                    snapshot,
                    tier,
                    ResumeResult(
                        ok=True,
                        content=landed_after_error.assistant_response or result.error,
                    ),
                    report,
                )
                return
            self._claims.mark_failed(claim.key, claim.claim_token, error=result.error)
            report.failed.append(
                TickOutcome(snapshot.session_id, "failed", result.error)
            )
            return

        # Verify the reply actually landed instead of trusting the exit code.
        # The app persists the turn shortly AFTER the CLI process exits, so
        # a single immediate read is a false failure; poll briefly instead.
        landed = self._await_new_turn(snapshot.session_id, claim.fingerprint.turn_index)
        if landed is None:
            self._claims.mark_failed(
                claim.key, claim.claim_token, error="no new turn recorded"
            )
            report.failed.append(
                TickOutcome(snapshot.session_id, "failed", "no new turn recorded")
            )
            return

        self._complete_success(claim, snapshot, tier, result, report)

    def _shadow_objects(self, snapshot: SessionSnapshot, detail: SessionDetail) -> str:
        """Ask the shadow whether answering this session is a bad idea.

        Returns the objection, or "" to proceed.

        No shadow configured means no objection -- a conductor the owner drives
        by hand does not need one, because he IS the brake. The unattended loop
        wires one; that is where it matters.

        A shadow that cannot answer BLOCKS. Failing open here would recreate
        precisely the hole that removing the old guard left: a loop writing into
        real sessions with nothing reviewing it, and no sign that the review
        never happened.
        """
        shadow = self._shadow
        if shadow is None:
            return ""
        last = detail.last_turn.assistant_response if detail.last_turn else ""
        try:
            verdict = shadow.review(
                session_id=snapshot.session_id,
                summary=snapshot.summary,
                cwd=snapshot.cwd,
                last_turn=last,
                next_steps=detail.next_steps,
            )
        except Exception as exc:  # noqa: BLE001 -- an unreachable critic is a block
            logger.warning("shadow review failed for %s: %s", snapshot.session_id, exc)
            return f"revisao indisponivel ({type(exc).__name__})"

        if verdict is None:
            return "revisao sem resposta"
        return "" if verdict.approved else (verdict.reason or "sem motivo declarado")

    def _complete_success(
        self,
        claim: Claim,
        snapshot: SessionSnapshot,
        tier: str,
        result: ResumeResult,
        report: TickReport,
    ) -> None:
        try:
            self._record_observation(claim, snapshot, tier, result)
        except Exception as exc:  # noqa: BLE001 - landed turns must not be retried
            self._claims.mark_succeeded(
                claim.key, claim.claim_token, detail=f"{tier}; memory failed"
            )
            report.failed.append(TickOutcome(snapshot.session_id, "failed", str(exc)))
            return

        self._claims.mark_succeeded(claim.key, claim.claim_token, detail=tier)
        report.answered.append(TickOutcome(snapshot.session_id, "answered", tier))
        self._announce(snapshot, result)

    def _record_observation(
        self,
        claim: Claim,
        snapshot: SessionSnapshot,
        tier: str,
        result: ResumeResult,
    ) -> None:
        recorder = self._observation_recorder
        if recorder is None:
            return
        recorder.record(
            session_id=snapshot.session_id,
            summary=snapshot.summary,
            tier=tier,
            response=result.content,
            fingerprint=claim.fingerprint.key,
            cwd=snapshot.cwd,
            repository=snapshot.repository,
        )

    def _await_new_turn(
        self, session_id: str, previous_index: int
    ) -> Optional[SessionTurn]:
        """Poll for the reply to appear, tolerating the store's write lag.

        The app persists the turn well after the CLI process exits -- measured at
        roughly a minute and a half on a resumed session. A short window here
        reports a false failure and, worse, burns a retry on work that actually
        succeeded.
        """
        deadline = time.monotonic() + self._readback_seconds
        while True:
            after = self._reader.detail(session_id)
            if (
                after is not None
                and after.last_turn is not None
                and after.last_turn.turn_index > previous_index
            ):
                return after.last_turn
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._readback_interval)

    def _promote_if_stuck(self, claim, snapshot, detail, report) -> bool:
        """Hand the session its own autonomy when it stalled on questions.

        A session that keeps asking is not waiting on work, it is waiting on a
        human who is not coming. Answering its questions one by one is exactly
        the chore the owner wants to stop doing, so the conductor turns on the
        panel that answers them instead.

        The switch lives in the target session's process, so it is asked to flip
        it; confirmation then reads the plugin's persisted state rather than the
        reply, because an agent can say "done" without having done it.
        """
        if self._promotion is None:
            return False

        turns = [t.assistant_response for t in (detail.recent_turns or [])]
        verdict = self._promotion.evaluate(
            snapshot.session_id, turns, autonomous=is_autonomous(snapshot.session_id)
        )
        if not verdict.should_promote:
            return False

        result = self._executor.resume(
            snapshot.session_id,
            PROMOTION_PROMPT,
            cwd=snapshot.cwd,
            turns=snapshot.turns,
        )
        if not result.ok:
            self._claims.mark_failed(claim.key, claim.claim_token, error=result.error)
            report.failed.append(
                TickOutcome(snapshot.session_id, "failed", result.error)
            )
            return True

        if not confirm_promotion(snapshot.session_id):
            # The session did not actually enable it. Release rather than fail:
            # nothing was accomplished, and the next tick should try again.
            self._claims.release(
                claim.key, claim.claim_token, reason="promotion not confirmed"
            )
            report.pending.append(
                TickOutcome(snapshot.session_id, "pending", "promotion unconfirmed")
            )
            return True

        self._claims.mark_succeeded(
            claim.key, claim.claim_token, detail="promoted to autonomy"
        )
        report.answered.append(
            TickOutcome(snapshot.session_id, "promoted", verdict.reason)
        )
        self._announce(snapshot, result)
        return True

    def _finish_conflict(
        self,
        claim: Claim,
        snapshot: SessionSnapshot,
        report: TickReport,
        reason: str,
    ) -> None:
        self._claims.mark_conflict(claim.key, claim.claim_token, detail=reason)
        report.conflicts.append(TickOutcome(snapshot.session_id, "conflict", reason))

    def _notify(self, message: str) -> None:
        """Tell the owner something, without letting the messenger break the tick."""
        if self._notifier is None:
            return
        try:
            self._notifier.notify(message)
        except Exception:  # noqa: BLE001 - a failed notification is not a failed tick
            logger.debug("conductor notification failed", exc_info=True)

    def _announce(self, snapshot: SessionSnapshot, result: ResumeResult) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(
                f"Retomei a sessao {snapshot.summary or snapshot.session_id}. "
                f"{result.content[:200]}"
            )
        except Exception:  # noqa: BLE001 - notification must not fail the tick
            logger.debug("conductor notification failed", exc_info=True)

    @staticmethod
    def _fingerprint(session_id: str, turn: SessionTurn) -> ObservationFingerprint:
        return ObservationFingerprint.from_turn(
            session_id, turn.turn_index, turn.timestamp, turn.content
        )


__all__ = [
    "ApprovalGate",
    "ConductorService",
    "DEFAULT_PROMPT",
    "Notifier",
    "ObservationRecorder",
    "ResumeExecutor",
    "ResumeResult",
    "SessionDetail",
    "SessionReader",
    "SessionTurn",
    "TickOutcome",
    "TickReport",
]
