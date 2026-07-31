"""Carry the owner's MCP servers into a headless session -- usable, not just visible.

Measured, and the reason this exists: a headless ``copilot`` child keeps its
built-in tools but gets **no MCP servers at all**. The CLI reads
``~/.copilot/mcp-config.json``, which on this machine is empty -- the owner's
servers live in the **mcp-bridge**, an extension of the *app*. No app, no
bridge, no MCP.

The failure is silent, which is what makes it dangerous: the session starts,
looks healthy, and only falls over later when it reaches for a tool that is not
there. A session opened to do work would simply do less of it, quietly.

Two flags are needed, not one. ``--additional-mcp-config`` makes the servers
exist; ``--allow-tool=<server>`` makes them callable. Also measured: with only
the config, the session LISTS the tools and then dies on invocation with
*"Permission denied and could not request permission from user"* -- a headless
child runs with ``--no-ask-user`` and has nobody to ask. A tool the agent can
see and plan around, but cannot call, is worse than one that was never there.

The config flag is additive and per-invocation on purpose: populating the global
``mcp-config.json`` would make the app and the CLI connect to the same servers
in parallel whenever the IDE is open. Permission is granted per SERVER and only
for servers the owner already configured -- nothing here widens what he had.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Where the mcp-bridge extension keeps the owner's servers.
BRIDGE_CONFIG = Path.home() / ".copilot" / "mcp-bridge" / "config.json"

#: Transports the CLI understands in an ``--additional-mcp-config`` payload.
_SUPPORTED = ("http", "sse", "local", "stdio")


def _why_unusable(server: Dict[str, Any]) -> str:
    """Why a bridge entry cannot serve a headless child, or "" when it can."""
    if not server.get("enabled", True):
        return "desligado no bridge"
    if (server.get("auth") or {}).get("type") == "oauth":
        # No browser here, so it would block on a consent screen nobody clicks.
        return "exige login pelo navegador"
    kind = str(server.get("type") or "").lower()
    if kind not in _SUPPORTED:
        return f"transporte nao suportado ({kind or 'sem tipo'})"
    if not (server.get("url") or server.get("command")):
        # The bridge resolves some servers through its own engine, which has no
        # address outside the app -- there is nothing for a child to dial.
        return "so existe dentro do app"
    return ""


def _translate(server: Dict[str, Any]) -> Dict[str, Any]:
    """Bridge entry -> the shape the CLI's mcp-config expects."""
    kind = str(server.get("type") or "").lower()
    if server.get("url"):
        entry: Dict[str, Any] = {"type": kind, "url": server["url"]}
    else:
        entry = {"type": "local", "command": server["command"]}
        if server.get("args"):
            entry["args"] = list(server["args"])
    if server.get("env"):
        entry["env"] = dict(server["env"])
    if server.get("headers"):
        entry["headers"] = dict(server["headers"])
    return entry


def read_bridge(source: Optional[Path] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(mcpServers, excluded)`` from the bridge config.

    The exclusions are returned, not merely logged, so a caller can SAY what a
    session will be missing. A tool that is simply absent is the failure this
    module exists to stop being silent, and a log line nobody reads is silence.

    Never raises. A missing or malformed bridge config means "no extra servers",
    which is the state a headless child would have had anyway -- refusing to
    launch over it would trade a degraded session for no session.
    """
    path = source or BRIDGE_CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("no mcp-bridge config at %s", path)
        return {"mcpServers": {}}, []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mcp-bridge config unreadable (%s): %s", path, exc)
        return {"mcpServers": {}}, [f"(config ilegivel: {exc})"]

    servers: Dict[str, Any] = {}
    excluded: List[str] = []
    for server in raw.get("servers", []):
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        reason = _why_unusable(server)
        if reason:
            excluded.append(f"{name} ({reason})")
        else:
            servers[name] = _translate(server)

    if excluded:
        logger.info("MCP servers not carried into headless: %s", ", ".join(excluded))
    return {"mcpServers": servers}, excluded


def build_config(source: Optional[Path] = None) -> Dict[str, Any]:
    """The ``mcpServers`` map alone, for callers that do not need the omissions."""
    return read_bridge(source)[0]


def excluded_servers(source: Optional[Path] = None) -> List[str]:
    """Servers that will NOT reach a headless session, each with its reason."""
    return read_bridge(source)[1]


def write_config(
    directory: Optional[str] = None, *, source: Optional[Path] = None
) -> Optional[str]:
    """Materialise the config and return its path, or None when there is nothing.

    Returning None rather than an empty file is deliberate: the caller can then
    leave the flag off entirely instead of pointing the CLI at a config that
    grants nothing, which looks like configuration that exists.
    """
    payload = build_config(source)
    if not payload["mcpServers"]:
        return None

    target = Path(directory) if directory else Path(tempfile.gettempdir())
    target.mkdir(parents=True, exist_ok=True)
    path = target / "jarvis-mcp-config.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if os.name == "posix":
        # The file drives what a session can reach. Keep it to the owner.
        path.chmod(0o600)
    return str(path)


def mcp_flags(
    directory: Optional[str] = None, *, source: Optional[Path] = None
) -> List[str]:
    """The argv fragment that makes the owner's MCP servers work in a child.

    Both flags or neither: config without permission yields tools that list and
    then refuse to run.
    """
    payload, _ = read_bridge(source)
    servers = list(payload["mcpServers"])
    if not servers:
        return []

    path = write_config(directory, source=source)
    if not path:
        return []

    return [f"--additional-mcp-config=@{path}"] + [
        f"--allow-tool={name}" for name in servers
    ]


__all__ = [
    "BRIDGE_CONFIG",
    "build_config",
    "excluded_servers",
    "mcp_flags",
    "read_bridge",
    "write_config",
]
