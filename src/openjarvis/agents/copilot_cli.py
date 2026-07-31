"""CopilotCliAgent -- wraps the official GitHub Copilot CLI (``copilot``).

Drives the ``copilot`` binary in non-interactive (``-p``) mode as a subprocess,
so inference is billed against the user's **GitHub Copilot subscription** rather
than a local model or a per-token API key. This keeps the machine's GPU free and
removes the need for any local LLM engine.

The ``engine`` parameter is accepted for :class:`BaseAgent` interface conformance
but is not used -- all inference happens inside the Copilot CLI.

Multi-turn continuity is preserved by capturing the session id the CLI prints in
its footer (``Resume  copilot --resume=<uuid>``) and passing ``--resume`` on
subsequent calls, so the agent keeps its context across ``run()`` invocations.

Environment note: the CLI authenticates from the ambient ``GITHUB_TOKEN`` /
``GH_TOKEN``, which must be left intact -- that is the account holding the
Copilot subscription. Stripping them makes the CLI fall back to an account
without quota and fail with "You have exceeded your monthly quota".
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.events import EventBus
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.utils import kill_process_tree
from openjarvis.engine._stubs import InferenceEngine

logger = logging.getLogger(__name__)

#: Matches the session id in the CLI footer line ``Resume  copilot --resume=<uuid>``.
#: Built-in tools that let a resumed session read, write or run things on
#: this machine. Excluding them BY NAME is the only filter measured to hold:
#: an empty `--available-tools=` is accepted and then ignored.
#:
#: Names the CLI does not recognise are reported and skipped, so the list can
#: stay ahead of a CLI version without breaking the run.
_SANDBOXED_TOOLS = (
    "view",
    "glob",
    "grep",
    "create",
    "edit",
    "powershell",
    "list_powershell",
    "read_powershell",
    "stop_powershell",
    "task",
    "web_fetch",
    "sql",
    "session_store_sql",
    "skill",
    "read_agent",
    "write_agent",
    "list_agents",
)

_RESUME_RE = re.compile(r"--resume=([0-9a-fA-F-]{36})")

#: First footer label emitted after the answer. Everything from here on is
#: telemetry chrome, not model output.
_FOOTER_START = "Changes"

#: Footer labels used to recognise the telemetry block (see :func:`_split_answer`).
_FOOTER_LABELS = ("Changes", "AI Credits", "Tokens", "Resume", "Total duration")


def is_copilot_cli_available() -> bool:
    """Return True if the ``copilot`` binary is on PATH."""
    return shutil.which("copilot") is not None


def _resolve_binary() -> Optional[str]:
    """Return the absolute path of the ``copilot`` launcher, or ``None``.

    The absolute path matters on Windows: ``copilot`` is an npm shim shipped as
    ``copilot.cmd`` / ``copilot.ps1``, and ``CreateProcess`` does not apply
    ``PATHEXT``, so spawning the bare name ``"copilot"`` raises ``FileNotFoundError``.
    ``shutil.which`` performs that resolution on every platform.
    """
    return shutil.which("copilot")


def _split_answer(stdout: str) -> tuple[str, dict[str, str]]:
    """Split raw CLI stdout into ``(answer, footer_fields)``.

    The CLI prints the model's answer first, then a blank-line-separated footer::

        Changes    +0 -0
        AI Credits 12.8 (10s)
        Tokens     ↑ 39.6k (20.8k cached, 18.8k written) • ↓ 14
        Resume     copilot --resume=<uuid>

    The footer is located by scanning for the *last* line that starts with
    ``Changes``, rather than the first, so an answer that merely mentions the
    word is not mistaken for the telemetry block.
    """
    lines = stdout.splitlines()

    cut = -1
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].startswith(_FOOTER_START):
            cut = idx
            break

    if cut == -1:
        return stdout.strip(), {}

    answer = "\n".join(lines[:cut]).strip()

    footer: dict[str, str] = {}
    for line in lines[cut:]:
        for label in _FOOTER_LABELS:
            if line.startswith(label):
                footer[label] = line[len(label) :].strip()
                break

    return answer, footer


@AgentRegistry.register("copilot_cli")
class CopilotCliAgent(BaseAgent):
    """Agent that delegates every turn to the GitHub Copilot CLI.

    Each :meth:`run` spawns ``copilot -p <prompt> --no-ask-user``. The first
    call starts a fresh CLI session; later calls reuse it via ``--resume`` so
    the conversation keeps its context (and benefits from prompt caching).

    Secure by default: with no extra configuration the CLI is launched with
    ``--available-tools=`` (an empty set, zero tools visible). The blanket
    ``--allow-all-tools`` flag is **never** added unless the caller explicitly
    passes ``allow_all_tools=True`` to the constructor.
    """

    agent_id = "copilot_cli"
    accepts_tools = False
    _default_temperature = 0.7
    _default_max_tokens = 1024

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        bus: Optional[EventBus] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        workspace: str = "",
        session_id: str = "",
        agent: str = "",
        allowed_dirs: Optional[List[str]] = None,
        no_ask_user: bool = True,
        timeout: int = 300,
        available_tools: Optional[List[str]] = None,
        excluded_tools: Optional[List[str]] = None,
        allow_tools: Optional[List[str]] = None,
        deny_tools: Optional[List[str]] = None,
        allow_all_tools: bool = False,
        sandboxed: bool = True,
        env: Optional[dict] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            bus=bus,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self._workspace = workspace or os.getcwd()
        self._session_id = session_id
        self._agent = agent
        self._allowed_dirs = allowed_dirs or []
        self._no_ask_user = no_ask_user
        self._timeout = timeout
        self._env = dict(env) if env else None

        self._allow_all_tools = allow_all_tools
        self._sandboxed = sandboxed
        self._available_tools = (
            list(available_tools) if available_tools is not None else []
        )
        self._excluded_tools = list(excluded_tools or [])
        self._allow_tools = list(allow_tools or [])
        self._deny_tools = list(deny_tools or [])

        if allow_all_tools:
            if available_tools is not None:
                raise ValueError(
                    "allow_all_tools=True is contradictory with an explicit "
                    "available_tools list: one grants every tool, the other "
                    "restricts to a fixed set. Pick one."
                )
            if "*" in self._deny_tools:
                raise ValueError(
                    "allow_all_tools=True is contradictory with deny_tools=['*']: "
                    "one grants every tool, the other denies everything."
                )

    @property
    def session_id(self) -> str:
        """CLI session id of the ongoing conversation ("" before the first run)."""
        return self._session_id

    def _build_command(self, prompt: str) -> List[str]:
        """Assemble the ``copilot`` argv for one turn.

        Secure by default: unless ``allow_all_tools=True`` was explicitly
        requested, the CLI is launched with ``--available-tools=`` (an empty
        set, zero tools visible) rather than the blanket ``--allow-all-tools``.
        No permission is ever inferred from ``prompt`` -- only from the
        agent's own constructor-time configuration.
        """
        cmd = [_resolve_binary() or "copilot", "-p", prompt]

        if self._allow_all_tools:
            cmd.append("--allow-all-tools")
        else:
            # `--available-tools=` with an EMPTY value is silently ignored by the
            # CLI: measured, the session still ran `List directory`. Denying tools
            # by name is what actually works, and it only holds together with
            # `--disable-builtin-mcps` -- with just the built-ins excluded, the
            # agent routed around them through the GitHub MCP server and read the
            # tree anyway. With both, the session answers that it cannot comply.
            if not self._sandboxed:
                # Resuming a session the owner already created: it keeps the
                # tools he granted it. Stripping them would leave the session
                # unable to continue its own work, which is the whole point of
                # resuming it. The conductor limits blast radius by choosing
                # WHICH sessions it answers, not by crippling them.
                pass
            elif self._available_tools:
                cmd.append(f"--available-tools={','.join(self._available_tools)}")
            else:
                cmd.append(f"--excluded-tools={','.join(_SANDBOXED_TOOLS)}")
                cmd.append("--disable-builtin-mcps")
        if self._excluded_tools:
            cmd.append(f"--excluded-tools={','.join(self._excluded_tools)}")
        for tool in self._allow_tools:
            cmd += ["--allow-tool", tool]
        for tool in self._deny_tools:
            cmd += ["--deny-tool", tool]

        cmd += ["--no-color"]
        cmd += ["--log-level", "none"]

        if self._session_id:
            cmd.append(f"--resume={self._session_id}")
        if self._model and self._model != "auto":
            cmd += ["--model", self._model]
        if self._agent:
            cmd += ["--agent", self._agent]
        if self._no_ask_user:
            cmd.append("--no-ask-user")
        for directory in self._allowed_dirs:
            cmd += ["--add-dir", directory]

        return cmd

    def _spawn(self, cmd: List[str]) -> "subprocess.CompletedProcess[str]":
        """Run ``cmd`` to completion, enforcing ``self._timeout``.

        Uses :func:`subprocess.Popen` (not :func:`subprocess.run`) so the
        spawned pid is available for :func:`~openjarvis.core.utils.kill_process_tree`
        if the timeout fires -- ``subprocess.run`` only ever kills the
        immediate child, orphaning any MCP/tool subprocesses it spawned.
        """
        # A child inherits this machine's CLI plugins and their hooks, so the
        # caller may need to hand it environment overrides (see the conductor).
        child_env = {**os.environ, **self._env} if self._env else None

        proc = subprocess.Popen(
            cmd,
            cwd=self._workspace,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Run one turn through the Copilot CLI and return its answer."""
        self._emit_turn_start(input)

        if not is_copilot_cli_available():
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=(
                    "CopilotCliAgent requires the GitHub Copilot CLI. Install it "
                    "with `npm install -g @github/copilot` and sign in with "
                    "`copilot login`."
                ),
                turns=1,
                metadata={"error": True, "error_type": "not_installed"},
            )

        try:
            proc = self._spawn(self._build_command(input))
        except subprocess.TimeoutExpired:
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=f"Copilot CLI timed out after {self._timeout}s.",
                turns=1,
                metadata={"error": True, "error_type": "timeout"},
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # When stdout is a pipe (the normal case here) the CLI keeps the two
        # streams clean: stdout carries only the model's answer, while the
        # telemetry footer -- including ``Resume  copilot --resume=<uuid>`` --
        # goes to stderr. Under a TTY both are interleaved on stdout, so scan
        # each stream and let :func:`_split_answer` strip any inline footer.
        match = _RESUME_RE.search(stderr) or _RESUME_RE.search(stdout)
        if match:
            self._session_id = match.group(1)

        if proc.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error("copilot CLI exited with %d: %s", proc.returncode, detail)
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=f"Copilot CLI failed: {detail}",
                turns=1,
                metadata={
                    "error": True,
                    "returncode": proc.returncode,
                    "session_id": self._session_id,
                },
            )

        answer, footer = _split_answer(stdout)
        if not footer:
            _, footer = _split_answer(stderr)

        metadata: dict[str, Any] = {"session_id": self._session_id}
        if footer:
            metadata["cli_footer"] = footer
        if self._model:
            metadata["model"] = self._model

        self._emit_turn_end(turns=1)
        return AgentResult(content=answer, turns=1, metadata=metadata)


__all__ = ["CopilotCliAgent", "is_copilot_cli_available"]
