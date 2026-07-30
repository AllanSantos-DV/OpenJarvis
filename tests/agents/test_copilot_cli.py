"""Unit tests for CopilotCliAgent (no CLI invocation required)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from openjarvis.agents.copilot_cli import CopilotCliAgent, _split_answer
from openjarvis.core.registry import AgentRegistry


@pytest.fixture(autouse=True)
def _register_copilot_cli():
    """Re-register after any registry clear."""
    if not AgentRegistry.contains("copilot_cli"):
        AgentRegistry.register_value("copilot_cli", CopilotCliAgent)


def _agent(**kwargs):
    return CopilotCliAgent(None, "auto", temperature=0.7, max_tokens=1024, **kwargs)


def _completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


FOOTER = (
    "Changes    +0 -0\n"
    "AI Credits 12.8 (10s)\n"
    "Resume     copilot --resume=429a3d23-55e9-41ee-9da4-4c65b9bbb54f\n"
)


def test_agent_is_registered():
    assert AgentRegistry.contains("copilot_cli")
    assert AgentRegistry.get("copilot_cli") is CopilotCliAgent


def test_split_answer_separates_footer():
    answer, footer = _split_answer("4\n\n\n" + FOOTER)

    assert answer == "4"
    assert footer["AI Credits"] == "12.8 (10s)"


def test_split_answer_keeps_answer_mentioning_footer_label():
    """The footer is found from the end, so prose may mention 'Changes'."""
    answer, _ = _split_answer("Changes were applied.\n\n" + FOOTER)

    assert answer == "Changes were applied."


def test_split_answer_without_footer_returns_whole_output():
    answer, footer = _split_answer("just text\n")

    assert answer == "just text"
    assert footer == {}


def test_build_command_uses_absolute_binary(monkeypatch):
    """CreateProcess ignores PATHEXT, so a bare name breaks the npm shim."""
    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli._resolve_binary", lambda: r"C:\npm\copilot.CMD"
    )
    cmd = _agent()._build_command("oi")

    assert cmd[0] == r"C:\npm\copilot.CMD"
    assert "--allow-all-tools" in cmd  # required for non-interactive mode


def test_build_command_omits_model_when_auto():
    assert "--model" not in _agent()._build_command("oi")


def test_build_command_passes_explicit_model():
    agent = CopilotCliAgent(None, "gpt-5.4", temperature=0.7, max_tokens=1024)
    cmd = agent._build_command("oi")

    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.4"


def test_build_command_resumes_known_session():
    cmd = _agent(session_id="abc-123")._build_command("oi")

    assert "--resume=abc-123" in cmd


def test_run_captures_session_id_from_stderr(monkeypatch):
    """Under a pipe the CLI writes the answer to stdout and the footer to stderr."""
    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli.is_copilot_cli_available", lambda: True
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(stdout="pong\n", stderr=FOOTER)
    )
    agent = _agent()

    result = agent.run("ping")

    assert result.content == "pong"
    assert agent.session_id == "429a3d23-55e9-41ee-9da4-4c65b9bbb54f"
    assert result.metadata["session_id"] == agent.session_id
    assert "AI Credits" not in result.content


def test_run_reports_failure_without_faking_an_answer(monkeypatch):
    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli.is_copilot_cli_available", lambda: True
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed(stderr="quota exceeded", returncode=1),
    )

    result = _agent().run("oi")

    assert result.metadata["error"] is True
    assert result.metadata["returncode"] == 1
    assert "quota exceeded" in result.content


def test_run_keeps_session_id_after_error(monkeypatch):
    """A transient failure must not lose the resumable conversation."""
    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli.is_copilot_cli_available", lambda: True
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed(stderr="boom\n" + FOOTER, returncode=1),
    )
    agent = _agent()

    agent.run("oi")

    assert agent.session_id == "429a3d23-55e9-41ee-9da4-4c65b9bbb54f"


def test_run_reports_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="copilot", timeout=1)

    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli.is_copilot_cli_available", lambda: True
    )
    monkeypatch.setattr(subprocess, "run", _raise)

    result = _agent(timeout=1).run("oi")

    assert result.metadata["error_type"] == "timeout"


def test_run_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(
        "openjarvis.agents.copilot_cli.is_copilot_cli_available", lambda: False
    )

    result = _agent().run("oi")

    assert result.metadata["error_type"] == "not_installed"
    assert "npm install -g @github/copilot" in result.content
