"""Ports for the conductor: the seams where I/O plugs into the pure core.

Keeping these as ``Protocol`` classes means the policy can be exercised against
in-memory fakes, and the real adapters (SQLite claim store, OS-level lock,
Copilot CLI executor) stay swappable and independently testable.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from openjarvis.conductor.models import (
    AuditEvent,
    Claim,
    ObservationFingerprint,
)


@runtime_checkable
class Clock(Protocol):
    """Injectable time source, so lease/backoff tests stay deterministic."""

    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


@runtime_checkable
class ClaimStore(Protocol):
    """Durable state for observations, leases and audit.

    Implementations MUST use storage owned by the conductor. Writing claim state
    into the Copilot app's own session store is forbidden: that schema belongs to
    another product and may change or be reset without notice.
    """

    def observe(self, fingerprint: ObservationFingerprint) -> Claim:
        """Record that *fingerprint* was seen, without claiming it."""

    def acquire(
        self,
        fingerprint: ObservationFingerprint,
        owner: str,
        *,
        lease_seconds: Optional[float] = None,
    ) -> Optional[Claim]:
        """Atomically take a lease, or return ``None`` when not claimable."""

    def mark_running(self, key: str, claim_token: str) -> Claim:
        """Move a leased claim into execution. Raises if the token is stale."""

    def mark_succeeded(self, key: str, claim_token: str, *, detail: str = "") -> Claim:
        """Terminal success for this fingerprint."""

    def mark_failed(self, key: str, claim_token: str, *, error: str = "") -> Claim:
        """Release the lease and schedule a retry while attempts remain."""

    def mark_conflict(self, key: str, claim_token: str, *, detail: str = "") -> Claim:
        """The session moved under us: stop acting on this fingerprint."""

    def recover_expired(self) -> List[Claim]:
        """Reclaim leases orphaned by a crash so work is not stuck forever."""

    def get(self, key: str) -> Optional[Claim]:
        """Current claim for *key*, or ``None`` if never observed."""

    def audit(self, limit: int = 100) -> List[AuditEvent]:
        """Recent structured audit events, newest first."""

    def close(self) -> None:
        """Release underlying resources."""


@runtime_checkable
class RuntimeLock(Protocol):
    """Process-wide singleton guard.

    Reading a config file cannot prevent two conductors from running: the check
    and the launch are separate steps. The guard must be held by the OS for as
    long as the process lives.
    """

    def acquire(self) -> bool:
        """Take the lock. ``False`` means another live process holds it."""

    def release(self) -> None:
        """Release the lock. Safe to call when not held."""

    @property
    def held(self) -> bool:
        """Whether this instance currently holds the lock."""


__all__ = ["ClaimStore", "Clock", "RuntimeLock"]
