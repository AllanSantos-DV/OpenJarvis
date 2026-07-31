"""The step between asking and building: deciding which one this is.

The brain answers and the arms open sessions, and until now nothing connected
them. ``CopilotCliAgent.accepts_tools`` is ``False`` -- it delegates a turn to
the CLI and returns the text -- so the brain could never *choose* to open a
session. The owner's sentence needs that choice:

    "quando eu falar pro Jarvis construir um produto, em vez de ele mesmo
     colocar um subagente pra fazer isso, ele vai abrir uma sessao dentro do
     GitHub Copilot IDE"

So: **conversation is answered, work is delegated**. Two different things the
owner says in the same voice, and telling them apart is the whole job here.

The decision is made by the brain itself -- one cheap classification turn --
rather than by keyword matching. "me explica como fazer um logger" and "faz um
logger" differ by intent, not by vocabulary, and a keyword list would send the
first one to build a session nobody asked for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Asked before anything else, and deliberately narrow: one word back.
_CLASSIFY = (
    "Voce e o roteador do Jarvis. O dono disse a frase abaixo. Ela e um PEDIDO "
    "DE TRABALHO em codigo (criar, alterar, corrigir, refatorar, implementar "
    "num projeto) ou e CONVERSA (pergunta, explicacao, opiniao, papo, pedido "
    "de status)? Responda UMA palavra: TRABALHO ou CONVERSA. Na duvida, "
    "responda CONVERSA -- abrir uma sessao que ninguem pediu custa mais caro "
    "do que responder uma pergunta. FRASE: "
)


@dataclass
class Decision:
    """What Jarvis decided to do with one thing the owner said."""

    kind: str  # "conversa" | "trabalho"
    answer: str = ""
    session_id: str = ""
    cwd: str = ""

    @property
    def delegated(self) -> bool:
        return self.kind == "trabalho"


class JarvisBrain:
    """Answers the owner, or hands the work to a session in his IDE."""

    def __init__(
        self,
        *,
        agent: Optional[Any] = None,
        ide: Optional[Any] = None,
        workspace: str = "",
    ) -> None:
        self._agent = agent
        self._ide = ide
        self._workspace = workspace

    def handle(self, said: str, *, cwd: str = "") -> Decision:
        """Route one utterance: answer it, or delegate it and report back."""
        if not said.strip():
            return Decision(kind="conversa", answer="")

        if self._classify(said) != "trabalho":
            return Decision(kind="conversa", answer=self._ask(said))

        where = cwd or self._workspace
        if not where:
            # Refusing beats guessing: opening a session in the wrong directory
            # means the work lands somewhere the owner will not look.
            return Decision(
                kind="conversa",
                answer=(
                    "Entendi como trabalho de codigo, mas nao sei em qual "
                    "projeto. Me diga a pasta e eu abro a sessao."
                ),
            )

        result = self._arms().execute(action="open", prompt=said, cwd=where)
        if not result.success:
            # A failed delegation is not a conversation -- say what broke.
            return Decision(kind="trabalho", answer=str(result.content), cwd=where)

        return Decision(
            kind="trabalho",
            answer=str(result.content),
            session_id=(result.metadata or {}).get("session_id", ""),
            cwd=where,
        )

    def follow(self, session_id: str, *, turns: int = 3) -> str:
        """Report on work already delegated, from the session's own record."""
        result = self._arms().execute(
            action="status", session_id=session_id, turns=turns
        )
        return str(result.content)

    # -- seams ---------------------------------------------------------------

    def _classify(self, said: str) -> str:
        """"trabalho" or "conversa". Never raises -- a router that breaks the
        conversation is worse than one that guesses conservatively.
        """
        try:
            answer = self._ask(_CLASSIFY + said)
        except Exception:  # noqa: BLE001
            logger.debug(
                "router could not classify; treating as conversa", exc_info=True
            )
            return "conversa"

        # The model was asked for one word and will sometimes write a sentence.
        # Look for the word, do not demand the whole answer be it.
        if re.search(r"\btrabalho\b", answer, re.IGNORECASE):
            return "trabalho"
        return "conversa"

    def _ask(self, prompt: str) -> str:
        agent = self._agent or self._default_agent()
        result = agent.run(prompt)
        if result.metadata.get("error"):
            raise RuntimeError(result.content)
        return (result.content or "").strip()

    def _arms(self):
        if self._ide is None:
            from openjarvis.tools.copilot_ide import CopilotIdeTool

            self._ide = CopilotIdeTool()
        return self._ide

    def _default_agent(self):
        from openjarvis.agents.copilot_cli import CopilotCliAgent
        from openjarvis.conductor.adapters import UNATTENDED_ENV

        self._agent = CopilotCliAgent(
            None, "auto", timeout=300, sandboxed=False, env=dict(UNATTENDED_ENV)
        )
        return self._agent


__all__ = ["Decision", "JarvisBrain"]
