"""The Jarvis brain's arms: open, drive and watch GitHub Copilot IDE sessions.

The owner does not want Jarvis writing the code itself. He wants Jarvis to open
a session in his IDE, hand it the job, and follow along -- so the work lands in
the same place he would find it if he had opened the session by hand, with the
tools he granted it.

**Transport.** Through the ``copilot`` CLI, not the copilot-mobile daemon. The
CLI and the app share ``~/.copilot/session-store.db``, so a session opened here
appears in the app and one opened in the app can be driven from here. The daemon
would be a nicer front door, but it is a separate process that can be down --
measured: its ``runtime.json`` pointed at a pid that no longer existed. The CLI
is always there, and this path is already proven end to end.

**What this is not.** It does not disarm the sessions it opens. They run with the
tools the owner granted, because a session that cannot use tools cannot do the
work that made it worth opening -- and because he must be able to take one over
by hand and finish it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.utils import kill_process_tree
from openjarvis.tools._stubs import BaseTool, ToolResult, ToolSpec
from openjarvis.tools.mcp_bridge_config import excluded_servers, mcp_flags

logger = logging.getLogger(__name__)

#: A child ``copilot`` inherits this machine's plugins and their hooks. The
#: voice-chat hook blocks any turn that does not call its ``falar`` tool, which
#: does not exist in a headless child -- the turn would burn its whole timeout
#: waiting for something it cannot do. Raising the hook's own threshold tells it
#: this turn is not a spoken summary, which is true: Jarvis speaks to the owner
#: himself.
_HEADLESS_ENV = {"VOICE_SUMMARY_MIN_CHARS": "1000000"}

#: Opening a session and getting the first answer costs more than a follow-up.
_OPEN_TIMEOUT = 600
_SEND_TIMEOUT = 900


def _run(
    args: List[str], *, prompt: str, cwd: str, timeout: int
) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        args,
        cwd=cwd or None,
        env={**os.environ, **_HEADLESS_ENV},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole tree: the session may have spawned MCP servers and tool
        # subprocesses that would otherwise outlive it.
        kill_process_tree(proc)
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


@ToolRegistry.register("copilot_ide")
class CopilotIdeTool(BaseTool):
    """Open a session in the Copilot IDE, give it work, and follow its progress."""

    tool_id = "copilot_ide"

    def __init__(self, *, store_path: Optional[str] = None) -> None:
        self._store_path = store_path

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="copilot_ide",
            description=(
                "Abre uma sessao no GitHub Copilot IDE, manda trabalho para ela e "
                "acompanha o andamento. Use action='open' para comecar um trabalho "
                "novo num diretorio, action='send' para dar mais uma instrucao a "
                "uma sessao existente, e action='status' para ler o estado e os "
                "ultimos turnos dela."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "send", "status"],
                        "description": "O que fazer com a sessao.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "A tarefa (open) ou a instrucao (send). Uma linha: "
                            "uma quebra de linha faz o --resume abrir uma sessao "
                            "NOVA em vez de continuar a existente."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Diretorio do projeto onde a sessao trabalha.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "A sessao alvo (send e status).",
                    },
                    "turns": {
                        "type": "integer",
                        "description": "Quantos turnos recentes trazer no status.",
                    },
                },
                "required": ["action"],
            },
        )

    def _fail(self, message: str, **metadata: Any) -> ToolResult:
        return ToolResult(
            tool_name=self.tool_id,
            success=False,
            content=message,
            metadata=metadata,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "").lower()
        handlers = {
            "open": self._open,
            "send": self._send,
            "status": self._status,
        }
        handler = handlers.get(action)
        if handler is None:
            return self._fail(
                f"Acao desconhecida: {action!r}. "
                f"Use uma de: {', '.join(sorted(handlers))}."
            )
        try:
            return handler(params)
        except subprocess.TimeoutExpired:
            return self._fail(
                "A sessao nao respondeu no tempo previsto. O processo foi "
                "encerrado; o trabalho ja feito continua na sessao."
            )
        except Exception as exc:  # noqa: BLE001 -- surface the real reason
            logger.exception("copilot_ide %s failed", action)
            return self._fail(f"{type(exc).__name__}: {exc}")

    def _open(self, params: Dict[str, Any]) -> ToolResult:
        prompt = _single_line(params.get("prompt"))
        if not prompt:
            return self._fail("Abrir uma sessao exige uma tarefa (prompt).")
        cwd = str(params.get("cwd") or os.getcwd())
        if not os.path.isdir(cwd):
            return self._fail(f"Diretorio inexistente: {cwd}")

        proc = _run(
            [_binary(), "--no-color", "--log-level", "none", "--no-ask-user"]
            + mcp_flags(),
            prompt=prompt,
            cwd=cwd,
            timeout=_OPEN_TIMEOUT,
        )
        session_id = _session_id_from(proc)
        if not session_id:
            return self._fail(
                "A sessao nao foi criada -- o CLI nao devolveu um id. "
                f"Saida: {(proc.stderr or proc.stdout or '').strip()[:400]}"
            )
        return ToolResult(
            tool_name=self.tool_id,
            success=True,
            content=_with_missing_tools(_answer(proc)),
            metadata={
                "session_id": session_id,
                "cwd": cwd,
                "mcp_excluded": excluded_servers(),
            },
        )

    def _send(self, params: Dict[str, Any]) -> ToolResult:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            return self._fail("Falta o session_id.")
        prompt = _single_line(params.get("prompt"))
        if not prompt:
            return self._fail("Falta a instrucao (prompt).")

        cwd = str(params.get("cwd") or "")
        if not cwd:
            detail = self._read(session_id)
            cwd = str((detail or {}).get("cwd") or os.getcwd())

        proc = _run(
            [
                _binary(),
                f"--resume={session_id}",
                "--no-color",
                "--log-level",
                "none",
                "--no-ask-user",
            ]
            + mcp_flags(),
            prompt=prompt,
            cwd=cwd,
            timeout=_SEND_TIMEOUT,
        )
        landed = _session_id_from(proc)
        if landed and landed != session_id:
            # The CLI opened a NEW session instead of appending -- the failure a
            # multi-line prompt causes. Say so plainly: silently reporting
            # success would leave the owner watching a session nothing reaches.
            return self._fail(
                f"A instrucao caiu numa sessao NOVA ({landed}) em vez de "
                f"continuar {session_id}. A sessao alvo nao recebeu nada.",
                session_id=landed,
                expected=session_id,
            )
        return ToolResult(
            tool_name=self.tool_id,
            success=True,
            content=_answer(proc),
            metadata={"session_id": session_id},
        )

    def _status(self, params: Dict[str, Any]) -> ToolResult:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            return self._fail("Falta o session_id.")
        detail = self._read(session_id, turns=int(params.get("turns") or 3))
        if detail is None:
            return self._fail(f"Sessao nao encontrada: {session_id}")
        return ToolResult(
            tool_name=self.tool_id,
            success=True,
            content=_describe(detail),
            metadata=detail,
        )

    def _read(self, session_id: str, *, turns: int = 3) -> Optional[Dict[str, Any]]:
        from openjarvis.tools.copilot_sessions import CopilotSessionsTool

        reader = CopilotSessionsTool(store_path=self._store_path)
        result = reader.execute(action="detail", session_id=session_id, turns=turns)
        if not result.success or not isinstance(result.content, dict):
            return None
        return dict(result.content)


def _with_missing_tools(answer: str) -> str:
    """Append what the session could NOT reach, so the gap is visible.

    ``excluded_servers`` has always known which MCP servers stay behind, but the
    knowledge lived in a log line. A session that quietly does less than asked is
    the failure this whole path exists to prevent, and a warning nobody reads
    prevents nothing -- so it rides back with the answer, where the caller (and
    the owner, through the notifier) actually sees it.
    """
    missing = excluded_servers()
    if not missing:
        return answer
    return f"{answer}\n\n[sem acesso a: {'; '.join(missing)}]"


def _binary() -> str:
    from openjarvis.agents.copilot_cli import _resolve_binary

    return _resolve_binary() or "copilot"


def _single_line(value: Any) -> str:
    """Collapse to one line.

    A prompt containing a newline makes ``--resume`` open a NEW session instead
    of appending to the target -- measured, and the cause of a day spent chasing
    turns that were landing somewhere else.
    """
    return " ".join(str(value or "").split())


def _session_id_from(proc: subprocess.CompletedProcess) -> str:
    from openjarvis.agents.copilot_cli import _RESUME_RE

    match = _RESUME_RE.search(proc.stderr or "") or _RESUME_RE.search(proc.stdout or "")
    return match.group(1) if match else ""


def _answer(proc: subprocess.CompletedProcess) -> str:
    from openjarvis.agents.copilot_cli import _split_answer

    return _split_answer(proc.stdout or "")[0].strip() or (proc.stderr or "").strip()


def _describe(detail: Dict[str, Any]) -> str:
    turns = detail.get("recent_turns") or []
    lines = [
        f"Sessao {detail.get('session_id', '?')} — "
        f"{detail.get('summary') or 'sem resumo'}",
        f"  pasta: {detail.get('cwd') or '?'}",
        f"  parada ha: {detail.get('idle_minutes', '?')} min",
        f"  atualizada: {detail.get('updated_at') or '?'}",
    ]
    files = detail.get("files_touched") or []
    if files:
        lines.append(f"  arquivos tocados: {len(files)}")
    for turn in turns:
        answer = " ".join(str(turn.get("assistant_response") or "").split())
        lines.append(f"  [{turn.get('turn_index')}] {answer[:200]}")
    return "\n".join(lines)
