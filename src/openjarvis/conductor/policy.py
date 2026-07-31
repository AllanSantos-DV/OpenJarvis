"""Eligibility and risk rules: which sessions the conductor may touch, and how.

These rules are deliberately deterministic and free of I/O. Deciding *whether to
act on someone else's session* is exactly the kind of decision that must not
depend on a model's mood, and it has to be testable without a database.

The eligibility filter is a safety device, not an optimisation. Without it the
conductor would see its own session in the idle list and start a conversation
with itself, and it could reply into CLI sessions that no human is watching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Sessions created by the Copilot desktop app. Anything else (notably the CLI's
#: own sessions, stored with an empty host type) is out of scope.
APP_HOST_TYPE = "github"

TIER_TRIVIAL = "trivial"
TIER_LOW = "low"
TIER_MEDIUM = "medium"
TIER_HIGH = "high"


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """What the conductor knows about one session at tick time."""

    session_id: str
    host_type: str = ""
    summary: str = ""
    cwd: str = ""
    repository: str = ""
    branch: str = ""
    turns: int = 0
    last_activity: str = ""
    idle_minutes: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Outcome of the safety filter, with the reason kept for the audit trail."""

    eligible: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EligibilityPolicy:
    """Decides whether a session may be acted upon at all."""

    own_session_id: str = ""
    idle_minutes: float = 10.0
    allowed_roots: Sequence[str] = field(default_factory=tuple)
    require_app_host: bool = True

    def evaluate(self, snapshot: SessionSnapshot) -> Eligibility:
        if self.require_app_host and snapshot.host_type != APP_HOST_TYPE:
            return Eligibility(False, f"host_type={snapshot.host_type or 'null'!s}")

        if self.own_session_id and snapshot.session_id == self.own_session_id:
            return Eligibility(False, "own session")

        if snapshot.idle_minutes is None:
            return Eligibility(False, "idle time unknown")

        if snapshot.idle_minutes < self.idle_minutes:
            return Eligibility(False, f"active {snapshot.idle_minutes:.1f}min ago")

        if self.allowed_roots and not self._under_allowed_root(snapshot.cwd):
            return Eligibility(False, f"cwd outside allowed roots: {snapshot.cwd}")

        return Eligibility(True, "eligible")

    def _under_allowed_root(self, cwd: str) -> bool:
        """Whether *cwd* sits under one of the configured roots.

        Comparison is case-insensitive and separator-normalised because the app
        records Windows paths with mixed separators and drive-letter casing.
        """
        if not cwd:
            return False
        normalised = cwd.replace("/", "\\").rstrip("\\").lower()
        for root in self.allowed_roots:
            candidate = str(root).replace("/", "\\").rstrip("\\").lower()
            if normalised == candidate or normalised.startswith(candidate + "\\"):
                return True
        return False


@dataclass(frozen=True, slots=True)
class TierPolicy:
    """Assigns a risk tier to answering a given session.

    Higher tiers demand human approval. The rules are conservative on purpose:
    the cost of asking one extra question is small, while auto-answering the
    wrong session is not recoverable.
    """

    #: Repositories or paths where the conductor may never act unattended.
    sensitive_markers: Sequence[str] = ("prod", "production", "infra", "secrets")
    #: Below this many turns a session is treated as a throwaway probe.
    trivial_max_turns: int = 2

    def classify(self, snapshot: SessionSnapshot, *, next_steps: str = "") -> str:
        haystack = " ".join(
            [snapshot.cwd or "", snapshot.repository or "", snapshot.branch or ""]
        ).lower()
        if any(marker in haystack for marker in self.sensitive_markers):
            return TIER_HIGH

        if snapshot.turns <= self.trivial_max_turns:
            return TIER_TRIVIAL

        # A session that recorded what it intends to do next is a routine
        # continuation; one that stopped without a plan needs a human look.
        return TIER_LOW if next_steps.strip() else TIER_MEDIUM


__all__ = [
    "APP_HOST_TYPE",
    "Eligibility",
    "EligibilityPolicy",
    "SessionSnapshot",
    "TIER_HIGH",
    "TIER_LOW",
    "TIER_MEDIUM",
    "TIER_TRIVIAL",
    "TierPolicy",
]
