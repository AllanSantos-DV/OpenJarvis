"""The brain's arms: opening, driving and watching IDE sessions.

These drive fakes, not the real CLI -- the live proof lives in the contract
tests. What they pin down is the behaviour that is easy to get wrong and
expensive when it is: a multi-line prompt silently landing in a NEW session
instead of the target, and a failure being reported as success.
"""

from __future__ import annotations

import subprocess

import pytest

from openjarvis.tools import copilot_ide
from openjarvis.tools.copilot_ide import CopilotIdeTool

FOOTER = "Resume     copilot --resume=11111111-2222-3333-4444-555555555555\n"
OTHER = "Resume     copilot --resume=99999999-8888-7777-6666-555555555555\n"
SID = "11111111-2222-3333-4444-555555555555"


class _Proc:
    """Stand-in for Popen: records what was piped in, replays a canned answer."""

    def __init__(self, stdout="", stderr="", *, sink=None, timeout=False):
        self.pid = 4242
        self.returncode = 0
        self._stdout = stdout
        self._stderr = stderr
        self._sink = sink
        self._timeout = timeout

    def communicate(self, input=None, timeout=None):
        if self._sink is not None:
            self._sink["stdin"] = input
        if self._timeout:
            raise subprocess.TimeoutExpired("copilot", timeout or 1)
        return (self._stdout, self._stderr)

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def spy(monkeypatch):
    """Capture argv and stdin of the spawned CLI, and script its reply."""
    seen = {}

    def _install(stdout="pronto\n", stderr=FOOTER, timeout=False):
        def _popen(args, **kwargs):
            seen["argv"] = args
            seen["cwd"] = kwargs.get("cwd")
            seen["env"] = kwargs.get("env")
            return _Proc(stdout, stderr, sink=seen, timeout=timeout)

        monkeypatch.setattr(subprocess, "Popen", _popen)
        return seen

    monkeypatch.setattr(copilot_ide, "_binary", lambda: "copilot")
    return _install


def test_open_returns_the_new_session_id(spy, tmp_path):
    seen = spy()
    result = CopilotIdeTool().execute(
        action="open", prompt="construa a fase 1", cwd=str(tmp_path)
    )

    assert result.success
    assert result.metadata["session_id"] == SID
    assert seen["cwd"] == str(tmp_path)
    assert "construa a fase 1" in seen["stdin"]


def test_open_refuses_a_directory_that_does_not_exist(spy, tmp_path):
    spy()
    result = CopilotIdeTool().execute(
        action="open", prompt="qualquer coisa", cwd=str(tmp_path / "nao-existe")
    )
    assert not result.success
    assert "inexistente" in result.content


def test_open_reports_failure_when_no_session_was_created(spy, tmp_path):
    # No footer means the CLI never got far enough to create a session. Saying
    # "ok" here would leave the brain holding an id it does not have.
    spy(stdout="", stderr="erro qualquer")
    result = CopilotIdeTool().execute(
        action="open", prompt="tarefa", cwd=str(tmp_path)
    )
    assert not result.success
    assert "erro qualquer" in result.content


def test_send_resumes_the_target_session(spy):
    seen = spy()
    result = CopilotIdeTool().execute(
        action="send", session_id=SID, prompt="continue", cwd="."
    )

    assert result.success
    assert f"--resume={SID}" in seen["argv"]


def test_send_reports_when_the_turn_landed_in_a_new_session(spy):
    """The failure that cost a day: the turn goes somewhere else, silently.

    A multi-line prompt makes the CLI open a fresh session instead of appending.
    The owner would then watch a session nothing ever reaches, while everything
    reported success.
    """
    spy(stderr=OTHER)
    result = CopilotIdeTool().execute(
        action="send", session_id=SID, prompt="continue", cwd="."
    )

    assert not result.success
    assert "sessao NOVA" in result.content
    assert result.metadata["expected"] == SID


def test_the_prompt_is_flattened_to_one_line(spy):
    seen = spy()
    CopilotIdeTool().execute(
        action="send",
        session_id=SID,
        prompt="primeira linha\nsegunda linha\n\nterceira",
        cwd=".",
    )

    assert "\n" not in seen["stdin"]
    assert "primeira linha segunda linha terceira" in seen["stdin"]


def test_the_child_gets_the_headless_environment(spy, tmp_path):
    # The voice-chat hook blocks any turn that does not call `falar`, a tool a
    # headless child does not have -- the turn would burn its whole timeout
    # waiting for something impossible.
    seen = spy()
    CopilotIdeTool().execute(action="open", prompt="tarefa", cwd=str(tmp_path))

    assert seen["env"]["VOICE_SUMMARY_MIN_CHARS"] == "1000000"


def test_a_timeout_is_reported_not_raised(spy, tmp_path):
    spy(timeout=True)
    result = CopilotIdeTool().execute(
        action="open", prompt="tarefa demorada", cwd=str(tmp_path)
    )
    assert not result.success
    assert "tempo previsto" in result.content


def test_unknown_action_lists_the_real_ones():
    result = CopilotIdeTool().execute(action="destruir")
    assert not result.success
    assert "open" in result.content and "send" in result.content


def test_open_carries_the_mcp_servers(spy, tmp_path, monkeypatch):
    """The guarantee has to live at the CALLSITE, not just in the helper.

    A headless child silently gets no MCP servers. The helper that fixes it can
    stay perfectly correct while a refactor drops the flag from the launch --
    and the failure is invisible: the session starts, looks fine, and quietly
    cannot reach half its tools.
    """
    monkeypatch.setattr(
        copilot_ide, "mcp_flags", lambda *a, **k: ["--additional-mcp-config=@x"]
    )
    seen = spy()
    CopilotIdeTool().execute(action="open", prompt="tarefa", cwd=str(tmp_path))

    assert "--additional-mcp-config=@x" in seen["argv"]


def test_send_carries_the_mcp_servers(spy, monkeypatch):
    # Resuming loses them just as easily as opening does.
    monkeypatch.setattr(
        copilot_ide, "mcp_flags", lambda *a, **k: ["--additional-mcp-config=@x"]
    )
    seen = spy()
    CopilotIdeTool().execute(action="send", session_id=SID, prompt="continue", cwd=".")

    assert "--additional-mcp-config=@x" in seen["argv"]


def test_no_servers_means_no_flag_at_the_callsite(spy, tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_ide, "mcp_flags", lambda *a, **k: [])
    seen = spy()
    CopilotIdeTool().execute(action="open", prompt="tarefa", cwd=str(tmp_path))

    assert not any("additional-mcp-config" in part for part in seen["argv"])


def test_the_answer_says_what_the_session_could_not_reach(spy, tmp_path, monkeypatch):
    """The gap has to arrive with the answer, not sit in a log.

    A session that quietly does less than it was asked is the failure this whole
    path exists to prevent -- and `excluded_servers` knowing the answer helps
    nobody if the knowledge never leaves the module.
    """
    monkeypatch.setattr(
        copilot_ide,
        "excluded_servers",
        lambda *a, **k: ["atlassian (exige login pelo navegador)"],
    )
    spy()
    result = CopilotIdeTool().execute(
        action="open", prompt="tarefa", cwd=str(tmp_path)
    )

    assert "sem acesso a" in result.content
    assert "atlassian" in result.content
    assert result.metadata["mcp_excluded"] == [
        "atlassian (exige login pelo navegador)"
    ]


def test_nothing_missing_means_no_noise(spy, tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_ide, "excluded_servers", lambda *a, **k: [])
    spy()
    result = CopilotIdeTool().execute(
        action="open", prompt="tarefa", cwd=str(tmp_path)
    )

    assert "sem acesso" not in result.content


def test_send_without_a_session_id_is_refused():
    result = CopilotIdeTool().execute(action="send", prompt="continue")
    assert not result.success
    assert "session_id" in result.content
