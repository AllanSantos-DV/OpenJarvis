"""Carry the owner's MCP servers into a headless session.

Measured, and the reason this exists: a headless ``copilot`` child keeps its
built-in tools but gets **no MCP servers at all**. The CLI reads
``~/.copilot/mcp-config.json``, which on this machine is empty -- the owner's
servers live in the **mcp-bridge**, an extension of the *app*. No app, no
bridge, no MCP.

The failure is silent, which is what makes it dangerous: the session starts
fine, looks healthy, and only falls over later when it reaches for a tool that
is not there. A session opened to do work would simply do less of it, quietly.

So every headless launch gets ``--additional-mcp-config`` built from the
bridge's own config. That flag is *additive and per-invocation*: populating the
global ``mcp-config.json`` instead would make the app and the CLI connect to the
same servers in parallel whenever the IDE is open.

Servers needing OAuth are skipped. A headless child has no browser and nobody to
approve a consent screen, so it would stall on a login it cannot complete.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Where the mcp-bridge extension keeps the owner's servers.
BRIDGE_CONFIG = Path.home() / ".copilot" / "mcp-bridge" / "config.json"

#: Transports the CLI understands in an ``--additional-mcp-config`` payload.
_SUPPORTED = ("http", "sse", "local", "stdio")


def _usable(server: Dict[str, Any]) -> bool:
    """Whether a bridge entry can serve a headless child.

    Disabled entries are out by definition. So is anything needing OAuth: a
    headless child has no browser, so it would block on a consent screen nobody
    is there to click.

    An entry with no URL and no command is out too -- the bridge resolves some
    servers through its own engine, which does not exist outside the app.
    """
    if not server.get("enabled", True):
        return False
    if (server.get("auth") or {}).get("type") == "oauth":
        return False
    if str(server.get("type") or "").lower() not in _SUPPORTED:
        return False
    return bool(server.get("url") or server.get("command"))


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


def build_config(source: Optional[Path] = None) -> Dict[str, Any]:
    """Read the bridge's servers and return a CLI-shaped ``mcpServers`` map.

    Never raises. A missing or malformed bridge config means "no extra servers",
    which is the state a headless child would have had anyway -- refusing to
    launch over it would trade a degraded session for no session.
    """
    path = source or BRIDGE_CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("no mcp-bridge config at %s", path)
        return {"mcpServers": {}}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mcp-bridge config unreadable (%s): %s", path, exc)
        return {"mcpServers": {}}

    servers: Dict[str, Any] = {}
    skipped: List[str] = []
    for server in raw.get("servers", []):
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        if _usable(server):
            servers[name] = _translate(server)
        else:
            skipped.append(name)

    if skipped:
        # Say which ones are missing. A tool that is simply absent is the exact
        # failure mode this module exists to stop being silent.
        logger.info("MCP servers not carried into headless: %s", ", ".join(skipped))
    return {"mcpServers": servers}


def write_config(
    directory: Optional[str] = None, *, source: Optional[Path] = None
) -> Optional[str]:
    """Materialise the config and return its path, or None when there is nothing.

    Returning None rather than an empty file is deliberate: the caller can then
    leave the flag off entirely instead of pointing the CLI at a config that
    grants nothing.
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


def mcp_flags(directory: Optional[str] = None) -> List[str]:
    """The ``--additional-mcp-config`` argv fragment, or empty when unavailable."""
    path = write_config(directory)
    return [f"--additional-mcp-config=@{path}"] if path else []


__all__ = ["BRIDGE_CONFIG", "build_config", "mcp_flags", "write_config"]
