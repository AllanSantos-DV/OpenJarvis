"""Process entry point: hold the singleton, tick, and report.

Startup deliberately fails fast on the things that silently produce a broken
conductor: a second instance already running, or a missing Copilot credential.
A missing voice engine is different -- it degrades the experience but not the
orchestration, so it only downgrades the notifier.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional, Sequence

from openjarvis.conductor.models import RetryPolicy
from openjarvis.conductor.policy import EligibilityPolicy, TierPolicy
from openjarvis.conductor.runtime_lock import FileRuntimeLock
from openjarvis.conductor.service import ConductorService, TickReport
from openjarvis.conductor.state import SqliteClaimStore

logger = logging.getLogger(__name__)

#: The CLI authenticates from the ambient token; without it every resume fails.
_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


class StartupError(RuntimeError):
    """Raised when the conductor must not start at all."""


def check_credentials(env: Optional[dict] = None) -> None:
    """Fail before ticking if the Copilot CLI has no token to authenticate with."""
    source = os.environ if env is None else env
    if not any(source.get(name) for name in _TOKEN_VARS):
        raise StartupError(
            "No GH_TOKEN or GITHUB_TOKEN in the environment. The Copilot CLI "
            "authenticates from it, so every resume would fail. Launch the "
            "conductor from a shell that has the subscription token."
        )


def build_service(
    *,
    own_session_id: str = "",
    idle_minutes: float = 10.0,
    allowed_roots: Sequence[str] = (),
    limit: int = 10,
    claims_path: Optional[str] = None,
    store_path: Optional[str] = None,
    dry_run: bool = False,
    speak: bool = True,
) -> ConductorService:
    """Wire the real adapters into the pure service."""
    from openjarvis.conductor.adapters import (
        ApprovalStoreGate,
        CopilotCliResumeExecutor,
        CopilotSessionsReader,
        LoggingNotifier,
        VoiceNotifier,
    )
    from openjarvis.conductor.service import ResumeResult

    class _DryRunExecutor:
        """Reports what it would do without touching any session."""

        def resume(self, session_id: str, prompt: str) -> ResumeResult:
            logger.info("[dry-run] would resume %s", session_id)
            return ResumeResult(ok=False, error="dry-run: nothing was sent")

    notifier = VoiceNotifier() if speak else LoggingNotifier()

    return ConductorService(
        reader=CopilotSessionsReader(store_path=store_path),
        claims=SqliteClaimStore(claims_path, policy=RetryPolicy()),
        executor=_DryRunExecutor() if dry_run else CopilotCliResumeExecutor(),
        eligibility=EligibilityPolicy(
            own_session_id=own_session_id,
            idle_minutes=idle_minutes,
            allowed_roots=tuple(allowed_roots),
        ),
        tiers=TierPolicy(),
        approvals=ApprovalStoreGate(),
        notifier=notifier,
        limit=limit,
    )


def run(
    service: ConductorService,
    *,
    lock: Optional[FileRuntimeLock] = None,
    interval: float = 0.0,
    max_ticks: Optional[int] = None,
) -> TickReport:
    """Run the conductor under the singleton lock.

    ``interval`` of zero runs a single tick and returns, which is what a
    scheduled task or a manual launch wants.
    """
    guard = lock or FileRuntimeLock()
    if not guard.acquire():
        raise StartupError(
            f"Another Jarvis conductor holds {guard.path}. Two conductors would "
            "answer the same session twice."
        )

    report = TickReport()
    ticks = 0
    try:
        while True:
            report = service.tick()
            ticks += 1
            if interval <= 0 or (max_ticks is not None and ticks >= max_ticks):
                return report
            time.sleep(interval)
    finally:
        guard.release()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point for the desktop launcher."""
    parser = argparse.ArgumentParser(
        prog="jarvis-conductor",
        description="Watch idle GitHub Copilot app sessions and keep them moving.",
    )
    parser.add_argument("--idle-minutes", type=float, default=10.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--interval", type=float, default=0.0, help="0 = single tick")
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument("--own-session-id", default=os.environ.get("SESSION_ID", ""))
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--claims-db", default=None)
    parser.add_argument("--session-store", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="do not speak results")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        check_credentials()
        service = build_service(
            own_session_id=args.own_session_id,
            idle_minutes=args.idle_minutes,
            allowed_roots=args.allowed_root,
            limit=args.limit,
            claims_path=args.claims_db,
            store_path=args.session_store,
            dry_run=args.dry_run,
            speak=not args.quiet,
        )
        report = run(
            service,
            interval=args.interval,
            max_ticks=args.max_ticks,
        )
    except StartupError as exc:
        print(f"jarvis-conductor: {exc}", file=sys.stderr)
        return 2

    print(f"jarvis-conductor: {report.summary()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
