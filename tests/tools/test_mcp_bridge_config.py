"""Carrying the owner's MCP servers into a headless session.

The failure this guards is silent: a headless child keeps its built-in tools but
gets no MCP servers, so it starts fine, looks healthy, and quietly does less
work than asked. These pin the translation and, more importantly, what gets
LEFT OUT -- an OAuth server dragged along would stall the child on a consent
screen nobody is there to click.
"""

from __future__ import annotations

import json

from openjarvis.tools.mcp_bridge_config import build_config, mcp_flags, write_config

BRIDGE = {
    "version": 1,
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
            "url": "https://mcp.atlassian.com/v1/sse",
            "auth": {"type": "oauth"},
            "enabled": True,
        },
        {
            "name": "mcp-gateway",
            "type": "http",
            "enabled": True,
            "engine": "mcp-gateway",
        },
        {
            "name": "desligado",
            "type": "http",
            "url": "http://127.0.0.1:1/mcp",
            "enabled": False,
        },
    ],
}


def _bridge(tmp_path, payload=BRIDGE):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_http_server_is_carried_over(tmp_path):
    servers = build_config(_bridge(tmp_path))["mcpServers"]
    assert servers["ata-ao-vivo"] == {
        "type": "http",
        "url": "http://127.0.0.1:19848/mcp",
    }


def test_an_oauth_server_is_left_behind(tmp_path):
    # A headless child has no browser: it would block on a consent screen
    # nobody is there to approve.
    assert "atlassian" not in build_config(_bridge(tmp_path))["mcpServers"]


def test_a_disabled_server_is_left_behind(tmp_path):
    assert "desligado" not in build_config(_bridge(tmp_path))["mcpServers"]


def test_a_server_with_no_reachable_address_is_left_behind(tmp_path):
    # The bridge resolves some servers through its own engine, which does not
    # exist outside the app -- there is no address for a child to dial.
    assert "mcp-gateway" not in build_config(_bridge(tmp_path))["mcpServers"]


def test_a_local_command_server_is_carried_over(tmp_path):
    path = _bridge(
        tmp_path,
        {
            "servers": [
                {
                    "name": "local",
                    "type": "local",
                    "command": "node",
                    "args": ["server.mjs"],
                    "env": {"TOKEN": "x"},
                    "enabled": True,
                }
            ]
        },
    )
    entry = build_config(path)["mcpServers"]["local"]
    assert entry["command"] == "node"
    assert entry["args"] == ["server.mjs"]
    assert entry["env"] == {"TOKEN": "x"}


def test_a_missing_bridge_config_is_not_fatal(tmp_path):
    # No bridge means no extra servers -- which is the state the child would
    # have had anyway. Refusing to launch would trade a degraded session for
    # no session.
    assert build_config(tmp_path / "nao-existe.json") == {"mcpServers": {}}


def test_a_corrupt_bridge_config_is_not_fatal(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ isto nao e json", encoding="utf-8")
    assert build_config(path) == {"mcpServers": {}}


def test_writing_produces_a_config_the_cli_can_read(tmp_path, monkeypatch):
    import openjarvis.tools.mcp_bridge_config as module

    monkeypatch.setattr(module, "BRIDGE_CONFIG", _bridge(tmp_path))
    written = write_config(str(tmp_path / "out"))

    assert written
    payload = json.loads(open(written, encoding="utf-8").read())
    assert "ata-ao-vivo" in payload["mcpServers"]


def test_nothing_to_carry_means_no_flag(tmp_path, monkeypatch):
    # Pointing the CLI at a config that grants nothing is worse than not
    # passing the flag: it looks like a configuration that exists.
    import openjarvis.tools.mcp_bridge_config as module

    monkeypatch.setattr(module, "BRIDGE_CONFIG", tmp_path / "vazio.json")
    assert write_config(str(tmp_path)) is None
    assert mcp_flags(str(tmp_path)) == []


def test_the_flag_points_at_the_written_file(tmp_path, monkeypatch):
    import openjarvis.tools.mcp_bridge_config as module

    monkeypatch.setattr(module, "BRIDGE_CONFIG", _bridge(tmp_path))
    (flag,) = mcp_flags(str(tmp_path / "out"))

    assert flag.startswith("--additional-mcp-config=@")
    assert "jarvis-mcp-config.json" in flag
