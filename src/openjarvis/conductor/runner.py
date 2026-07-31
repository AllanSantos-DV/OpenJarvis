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
import shutil
import sys
import time
from typing import Optional, Sequence

from openjarvis.conductor.models import RetryPolicy
from openjarvis.conductor.policy import EligibilityPolicy, TierPolicy
from openjarvis.conductor.promotion import PromotionPolicy
from openjarvis.conductor.runtime_lock import FileRuntimeLock
from openjarvis.conductor.service import ConductorService, TickReport
from openjarvis.conductor.state import SqliteClaimStore

logger = logging.getLogger(__name__)

#: The CLI authenticates from the ambient token OR from a credential the owner
#: stored with `copilot login`. Checking for the variable alone rejects a
#: perfectly working setup.
_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")

#: A trivial prompt: enough to prove the CLI can authenticate and spend, cheap
#: enough to run before every launch.
_AUTH_PROBE = "Responda somente: ok"
_AUTH_TIMEOUT = 90


class StartupError(RuntimeError):
    """Raised when the conductor must not start at all."""


def check_credentials(env: Optional[dict] = None, *, probe: bool = False) -> None:
    """Fail before ticking if the Copilot CLI cannot authenticate.

    Two ways it can. An ambient ``GH_TOKEN``/``GITHUB_TOKEN``, or a credential
    the owner stored with ``copilot login``. An earlier version demanded the
    variable and nothing else, which rejected a working machine: the desktop
    shortcut opens a clean shell, and that token is injected by the Copilot app
    into ITS process -- it is not a user-level variable, so the shortcut could
    never have it.

    That is the same mistake as probing a port instead of the process that
    should own it: testing for a SYMPTOM of the capability rather than the
    capability. With ``probe=True`` the check spends one trivial prompt and
    learns the answer for real.
    """
    source = os.environ if env is None else env
    has_var = any(source.get(name) for name in _TOKEN_VARS)

    if probe and _cli_authenticates():
        return
    if has_var:
        return

    raise StartupError(
        "O Copilot CLI nao conseguiu autenticar. Duas saidas:\n"
        "  1) rode `copilot login` uma vez -- a credencial fica no cofre do "
        "Windows e o atalho passa a funcionar sozinho;\n"
        "  2) ou inicie o conductor de um shell que tenha GH_TOKEN/GITHUB_TOKEN "
        "da conta com assinatura.\n"
        "Sem isso a sessao cai numa conta sem cota e todo resume falha."
    )


def _cli_authenticates() -> bool:
    """Whether ``copilot`` can actually answer right now.

    Cheap and decisive: a machine without quota answers "You have exceeded your
    monthly quota" instead of the prompt, which is exactly the failure this is
    meant to catch before a whole tick is wasted on it.
    """
    import subprocess

    binary = shutil.which("copilot")
    if not binary:
        return False
    try:
        out = subprocess.run(
            [binary, "-p", _AUTH_PROBE, "--no-ask-user", "--no-color"],
            capture_output=True,
            text=True,
            timeout=_AUTH_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:  # noqa: BLE001 -- a failed probe is not proof of failure
        logger.debug("auth probe could not run", exc_info=True)
        return False
    combined = f"{out.stdout or ''}{out.stderr or ''}".lower()
    if "quota" in combined or "unauthor" in combined or "not logged in" in combined:
        return False
    return "ok" in (out.stdout or "").lower()



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
    resume_timeout: int = 120,
    shadow: bool = True,
) -> ConductorService:
    """Wire the real adapters into the pure service."""
    from openjarvis.conductor.adapters import (
        ApprovalStoreGate,
        CopilotCliResumeExecutor,
        CopilotSessionsReader,
        LoggingNotifier,
        NativeJavaObservationRecorder,
        VoiceNotifier,
    )
    from openjarvis.conductor.mirror import SessionShadow
    from openjarvis.conductor.service import ResumeResult

    class _DryRunExecutor:
        """Reports what it would do without touching any session."""

        def resume(
            self, session_id: str, prompt: str, *, cwd: str = ""
        ) -> ResumeResult:
            logger.info("[dry-run] would resume %s", session_id)
            return ResumeResult(ok=False, error="dry-run: nothing was sent")

    notifier = VoiceNotifier() if speak else LoggingNotifier()

    return ConductorService(
        reader=CopilotSessionsReader(store_path=store_path),
        claims=SqliteClaimStore(claims_path, policy=RetryPolicy()),
        # A resumed session answers in roughly a minute; a much larger budget
        # only means one slow session eats the whole tick, and the CLI keeps
        # running while every other stalled session waits its turn.
        executor=(
            _DryRunExecutor()
            if dry_run
            else CopilotCliResumeExecutor(timeout=resume_timeout)
        ),
        eligibility=EligibilityPolicy(
            own_session_id=own_session_id,
            idle_minutes=idle_minutes,
            allowed_roots=tuple(allowed_roots),
        ),
        tiers=TierPolicy(),
        # A session that stalled asking gets the owner's own autonomy panel
        # turned on instead of having its questions answered one by one.
        promotion=PromotionPolicy(),
        # The brake. Nobody is watching what this loop writes into real
        # sessions, so something has to ask "should this be answered now?"
        # before each turn goes out. A dry run needs none -- it writes nothing.
        shadow=None if (dry_run or not shadow) else SessionShadow(),
        approvals=ApprovalStoreGate(),
        notifier=notifier,
        observation_recorder=None if dry_run else NativeJavaObservationRecorder(),
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
    parser.add_argument("--resume-timeout", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="do not speak results")
    parser.add_argument(
        "--no-shadow",
        action="store_true",
        help=(
            "run WITHOUT the shadow review. Only for a run the owner is "
            "watching -- unattended, the shadow is the only thing between the "
            "loop and a wrong turn in a real session."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        check_credentials(probe=True)
        service = build_service(
            own_session_id=args.own_session_id,
            idle_minutes=args.idle_minutes,
            allowed_roots=args.allowed_root,
            limit=args.limit,
            claims_path=args.claims_db,
            store_path=args.session_store,
            dry_run=args.dry_run,
            speak=not args.quiet,
            resume_timeout=args.resume_timeout,
            shadow=not args.no_shadow,
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
