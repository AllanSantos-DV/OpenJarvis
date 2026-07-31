"""What a double-click on the desktop shortcut actually does.

This exists because of a regression that shipped: a "safe default" was added to
the launcher so that, without ``-AutoAnswer``, it forced report mode. The
shortcut passes no arguments -- so the one path the owner uses stopped
conducting and started merely reporting. Nothing failed, no test went red, and
the product silently became a status printout.

The click IS the consent. What the default restrains is scope (which folders)
and reach (the tier gate), never whether the conductor does its job.

``-PrintPlan`` resolves the invocation and exits before any probe, so these run
without a Copilot token, the CLI, or a voice engine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "jarvis-conductor.ps1"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the launcher is a PowerShell script"
)


def _plan(*args: str) -> str:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-PrintPlan",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _roots(plan: str) -> list[str]:
    parts = plan.split()
    return [parts[i + 1] for i, part in enumerate(parts) if part == "--allowed-root"]


DEFAULT_ROOT = SCRIPT.resolve().parents[2].parent


def test_the_shortcut_path_conducts():
    # No arguments is exactly what the .lnk passes.
    assert "--dry-run" not in _plan()


def test_the_shortcut_path_is_scoped():
    # Scoping to the checkout itself would exclude the sibling projects whose
    # sessions are the whole point, so the default is one level up.
    assert _roots(_plan()) == [str(DEFAULT_ROOT)]


def test_report_only_does_not_answer():
    assert "--dry-run" in _plan("-ReportOnly")


def test_dry_run_still_works_as_an_alias():
    assert "--dry-run" in _plan("-DryRun")


def test_explicit_scope_replaces_the_default():
    assert _roots(_plan("-AllowedRoot", "C:\\tmp")) == ["C:\\tmp"]
