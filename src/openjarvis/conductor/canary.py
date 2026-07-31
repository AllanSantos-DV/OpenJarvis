"""Boot canary: re-check the environment invariants each time the conductor starts.

Everything here depends on things outside this repository -- the Copilot CLI, the
owner's mcp-bridge, the app's session store. Those move on their own: a CLI
update can change a flag's behaviour, a bridge edit can drop a server, an
expired credential can turn every resume into a no-op. The suite cannot see any
of that.

Neither can CI. The contract tests that DO exercise these paths are skipped
unless ``RUN_COPILOT_CONTRACT=1``, and the hosted runner has no Copilot CLI, no
subscription and no bridge config -- so they never run there and never will.
A guarantee that only holds when someone remembers to run it by hand is the
same failure this project keeps paying for: the check exists, and nothing makes
it happen.

So the check runs where the truth is: on this machine, at the moment the
conductor starts, before it touches a session. It is deliberately cheap and
NON-DESTRUCTIVE -- it must never kill a process, expire a token or write into a
session to learn something.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: The canary spends one small prompt. Anything slower would tax every launch.
_TIMEOUT = 120


@dataclass
class CanaryReport:
    """What the environment looked like at boot."""

    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.warnings

    def summary(self) -> str:
        if self.ok:
            return "ambiente conferido: " + ("; ".join(self.notes) or "sem observacoes")
        return "ATENCAO -- " + "; ".join(self.warnings)


def run_canary(*, agent_factory: Optional[Any] = None) -> CanaryReport:
    """Check what the conductor is about to rely on. Never raises."""
    report = CanaryReport()
    _check_mcp(report, agent_factory)
    return report


def _check_mcp(report: CanaryReport, agent_factory: Optional[Any]) -> None:
    """Are the owner's MCP servers reachable AND callable from a headless child?

    Both halves matter and only invocation proves the second. A config-only
    launch grants tools the agent can list, plan around and never use -- that
    exact regression shipped once, and listing looked fine while it did.
    """
    from openjarvis.tools.mcp_bridge_config import excluded_servers, read_bridge

    payload, excluded = read_bridge()
    servers = list(payload["mcpServers"])
    if excluded:
        report.notes.append(f"MCP fora do headless: {', '.join(excluded)}")
    if not servers:
        report.notes.append("nenhum MCP a carregar")
        return

    probe = _probe_tool(servers, agent_factory)
    if probe is None:
        report.notes.append(f"MCP carregado: {', '.join(servers)} (nao exercitado)")
        return
    if not probe:
        report.warnings.append(
            f"os MCP ({', '.join(servers)}) aparecem mas NAO executam -- "
            "uma sessao vai planejar com ferramentas que nao pode chamar"
        )
        return
    report.notes.append(f"MCP carregado e chamavel: {', '.join(servers)}")
    # Keep the exclusions visible on the happy path too: a server that silently
    # stopped being carried is the failure this reports.
    if excluded_servers() != excluded:
        report.notes.append("lista de exclusoes mudou durante o boot")


def _probe_tool(servers: List[str], agent_factory: Optional[Any]) -> Optional[bool]:
    """True/False when the probe ran, None when it could not be attempted.

    ``None`` is not a failure: without the CLI on PATH there is nothing to test
    and the conductor will fail for a clearer reason moments later.
    """
    try:
        agent = (agent_factory or _default_agent)()
    except Exception:  # noqa: BLE001
        logger.debug("canary could not build an agent", exc_info=True)
        return None

    # Ask about a NAMED server, not a tool count. Measured: a plain headless
    # child already reports 5 MCP tools, inherited from the machine's installed
    # plugins -- so a count is above zero whether or not the owner's servers
    # arrived, and a canary built on it would report "fine" while the injection
    # was completely broken. Naming the server discriminates: without the flags
    # the same child answers NAO, with them SIM.
    #
    # Tool NAMES are not usable here either: this server exposes `ata_estado`,
    # not `ata_ao_vivo_estado`, so matching by prefix guesses wrong.
    target = servers[0]
    try:
        result = agent.run(
            f"Voce tem acesso a alguma ferramenta do servidor MCP chamado "
            f"'{target}'? Responda apenas SIM ou NAO."
        )
    except Exception:  # noqa: BLE001 -- a canary must never break the launch
        logger.debug("canary probe failed to run", exc_info=True)
        return None

    if result.metadata.get("error"):
        return None
    answer = (result.content or "").strip().upper()
    if answer.startswith("SIM"):
        return True
    if answer.startswith("NAO") or answer.startswith("NÃO"):
        return False
    logger.debug("canary probe answered something else: %s", answer[:120])
    return None


def _default_agent():
    from openjarvis.agents.copilot_cli import CopilotCliAgent
    from openjarvis.conductor.adapters import UNATTENDED_ENV

    return CopilotCliAgent(
        None,
        "auto",
        timeout=_TIMEOUT,
        sandboxed=False,
        env=dict(UNATTENDED_ENV),
    )


__all__ = ["CanaryReport", "run_canary"]
