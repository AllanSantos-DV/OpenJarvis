"""Pure domain models for the Jarvis conductor.

No I/O lives here: these types describe *what* the conductor observes and how a
unit of work moves between states, so the policy can be tested without a
database, a CLI or a running Copilot app.

The central idea is the :class:`ObservationFingerprint`. Identifying a unit of
work by ``(session_id, turn_index)`` alone is not enough -- an app session can
rewrite or replace a turn, and a stale pair would silently authorise a reply to
content that no longer exists. Including the timestamp and a hash of the content
makes the identity describe *the exact turn we decided to act on*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Final

#: A unit of work that has been seen but not yet claimed.
OBSERVED: Final = "observed"
#: Claimed by one owner, which holds a time-bounded lease.
LEASED: Final = "leased"
#: The owner is executing the side effect (spawning the CLI, replying).
RUNNING: Final = "running"
#: Terminal success: never retried, and safe to observe again idempotently.
SUCCEEDED: Final = "succeeded"
#: Failed or timed out. Retryable while attempts remain -- a crash must not
#: burn a turn forever.
FAILED: Final = "failed"
#: Someone else changed the session under us. Terminal for *this* fingerprint;
#: a new fingerprint (new turn) starts fresh.
CONFLICT: Final = "conflict"

TERMINAL_STATES: Final = frozenset({SUCCEEDED, CONFLICT})
ACTIVE_STATES: Final = frozenset({LEASED, RUNNING})


class ConductorError(RuntimeError):
    """Base error for conductor state transitions."""


class StaleClaimError(ConductorError):
    """Raised when a worker tries to advance a claim it no longer owns.

    This happens after a lease expires and the work is handed to another owner:
    the original worker must not be able to mark the new owner's claim as
    finished, or a reply could be recorded that never happened.
    """


@dataclass(frozen=True, slots=True)
class ObservationFingerprint:
    """Identity of one observed turn, stable across ticks but not across edits."""

    session_id: str
    turn_index: int
    timestamp: str
    content_hash: str

    @classmethod
    def from_turn(
        cls,
        session_id: str,
        turn_index: int,
        timestamp: str,
        content: str,
    ) -> "ObservationFingerprint":
        """Build a fingerprint, hashing *content* so edits produce a new identity."""
        digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        return cls(
            session_id=session_id,
            turn_index=int(turn_index),
            timestamp=timestamp or "",
            content_hash=digest,
        )

    @property
    def key(self) -> str:
        """Stable primary key for this exact observation."""
        return (
            f"{self.session_id}:{self.turn_index}:{self.timestamp}:{self.content_hash}"
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often a failed observation may be retried, and for how long a lease holds."""

    max_attempts: int = 3
    lease_seconds: float = 300.0
    backoff_seconds: float = 60.0

    def backoff_for(self, attempts: int) -> float:
        """Exponential backoff, capped so a stuck session cannot park forever."""
        exponent = max(0, attempts - 1)
        return float(self.backoff_seconds * (2 ** min(exponent, 4)))


@dataclass(frozen=True, slots=True)
class Claim:
    """A unit of work plus the lease that authorises acting on it."""

    fingerprint: ObservationFingerprint
    state: str = OBSERVED
    owner: str = ""
    claim_token: str = ""
    lease_expires_at: float = 0.0
    attempts: int = 0
    retry_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.fingerprint.key

    @property
    def session_id(self) -> str:
        return self.fingerprint.session_id

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def lease_active_at(self, now: float) -> bool:
        """True while another owner is legitimately working on this claim."""
        return self.state in ACTIVE_STATES and self.lease_expires_at > now


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One structured audit row, correlated by fingerprint key."""

    timestamp: float
    fingerprint_key: str
    session_id: str
    event: str
    owner: str = ""
    claim_token: str = ""
    detail: str = ""


def make_owner_id(prefix: str = "jarvis") -> str:
    """Identify this process for lease ownership and audit correlation."""
    import os
    import socket

    return f"{prefix}@{socket.gethostname()}:{os.getpid()}"


__all__ = [
    "ACTIVE_STATES",
    "AuditEvent",
    "CONFLICT",
    "Claim",
    "ConductorError",
    "FAILED",
    "LEASED",
    "OBSERVED",
    "ObservationFingerprint",
    "RUNNING",
    "RetryPolicy",
    "SUCCEEDED",
    "StaleClaimError",
    "TERMINAL_STATES",
    "make_owner_id",
]
