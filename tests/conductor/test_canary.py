"""The boot canary.

Its job is to notice, before the conductor touches a session, that something
outside this repository moved -- a CLI update, an edited bridge, an expired
credential. The suite cannot see any of that, and CI has neither the CLI nor
the owner's config, so the contract tests that DO exercise these paths never
run there. This is the check that actually happens.
"""

from __future__ import annotations

from openjarvis.conductor.canary import run_canary


class _Answer:
    def __init__(self, content, error=False):
        self.content = content
        self.metadata = {"error": True} if error else {}


class _Agent:
    def __init__(self, answer):
        self._answer = answer
        self.calls = 0

    def run(self, prompt):
        self.calls += 1
        return self._answer


BRIDGE = {
    "servers": [
        {
            "name": "ata-ao-vivo",
            "type": "http",
            "url": "http://127.0.0.1:19848/mcp",
            "enabled": True,
        },
        {
            "name": "atlassian",
            "type": "sse",
            "url": "https://x/sse",
            "auth": {"type": "oauth"},
            "enabled": True,
        },
    ]
}


def _bridge(tmp_path, monkeypatch, payload=BRIDGE):
    import json

    import openjarvis.tools.mcp_bridge_config as module

    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(module, "BRIDGE_CONFIG", path)


def test_a_reachable_mcp_server_is_reported_as_callable(tmp_path, monkeypatch):
    _bridge(tmp_path, monkeypatch)
    agent = _Agent(_Answer("SIM"))

    report = run_canary(agent_factory=lambda: agent)

    assert report.ok
    assert any("chamavel" in note for note in report.notes)


def test_tools_that_vanished_are_a_warning(tmp_path, monkeypatch):
    """The regression that shipped once, caught at boot instead of mid-work.

    A config-only launch grants tools the agent can list, plan around and never
    call. Here the child reports none at all -- the other half of the same
    failure, and just as silent while the session runs.
    """
    _bridge(tmp_path, monkeypatch)
    agent = _Agent(_Answer("NAO"))

    report = run_canary(agent_factory=lambda: agent)

    assert not report.ok
    assert "NAO executam" in report.warnings[0]


def test_excluded_servers_are_always_named(tmp_path, monkeypatch):
    _bridge(tmp_path, monkeypatch)
    agent = _Agent(_Answer("SIM"))

    report = run_canary(agent_factory=lambda: agent)

    assert any("atlassian" in note for note in report.notes)


def test_a_probe_that_cannot_run_is_not_a_failure(tmp_path, monkeypatch):
    """No CLI on PATH is not evidence of anything.

    The conductor fails for a clearer reason moments later; a canary that cried
    wolf here would train the owner to ignore it.
    """
    _bridge(tmp_path, monkeypatch)

    def _explode():
        raise RuntimeError("copilot nao instalado")

    report = run_canary(agent_factory=_explode)

    assert report.ok
    assert any("nao exercitado" in note for note in report.notes)


def test_an_agent_error_is_not_a_failure_either(tmp_path, monkeypatch):
    _bridge(tmp_path, monkeypatch)
    agent = _Agent(_Answer("quota estourada", error=True))

    report = run_canary(agent_factory=lambda: agent)

    assert report.ok


def test_nothing_to_carry_skips_the_probe(tmp_path, monkeypatch):
    _bridge(tmp_path, monkeypatch, {"servers": []})
    agent = _Agent(_Answer("qualquer coisa"))

    report = run_canary(agent_factory=lambda: agent)

    assert report.ok
    assert agent.calls == 0
