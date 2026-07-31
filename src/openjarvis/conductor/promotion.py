"""Promoting a stalled session to autonomy, with a verifiable outcome.

Some sessions do not stop because the work ended -- they stop because the agent
keeps asking. The owner already built the answer to that: the ``modo-auto``
plugin, whose panel of agents answers those questions so the session finishes its
phases on its own. Turning it on is therefore the right move for a session that
is stuck asking rather than stuck working.

Two constraints shape this module, and both come from how the plugin actually
works:

* **The conductor cannot flip the switch.** The toggle lives inside the target
  session's own process, keyed by its session id, and enabling it swaps the
  session's tools, which needs a re-join that only that process can perform. So
  the conductor asks the session to run ``modo_auto on`` itself.
* **Asking is not the same as it happening.** A natural-language instruction may
  be ignored, misread, or answered with a polite "done" that never happened.
  Confirmation therefore reads the plugin's own persisted state
  (``~/.copilot-modo-auto/<session_id>.json``), not the agent's reply.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

_STATE_DIR_ENV = "COPILOT_MODO_AUTO_DIR"
_DEFAULT_STATE_DIR = Path.home() / ".copilot-modo-auto"

#: Single line on purpose: a prompt containing newlines makes ``--resume`` open a
#: new session instead of continuing the target one.
PROMOTION_PROMPT = (
    "O Jarvis notou que esta sessao esta parando para perguntar em vez de "
    "avancar, e o dono nao esta olhando agora. Ligue o modo autonomo desta "
    "sessao chamando a tool modo_auto com action on, para a mesa responder as "
    "perguntas e o trabalho seguir. Depois confirme em uma linha."
)

#: How many questions in the recent turns count as "stuck asking" rather than
#: "asking once, legitimately". One question is a session doing its job; a
#: session that asked repeatedly and then went idle is waiting on a human who is
#: not coming.
DEFAULT_QUESTION_THRESHOLD = 2

_QUESTION_HINTS = (
    "?",
    "posso prosseguir",
    "quer que eu",
    "devo seguir",
    "confirma",
    "qual das opcoes",
    "qual das opções",
)


def state_dir() -> Path:
    """Where the modo-auto plugin persists its per-session toggles."""
    override = os.environ.get(_STATE_DIR_ENV)
    return Path(override) if override else _DEFAULT_STATE_DIR


def is_autonomous(session_id: str) -> Optional[bool]:
    """Read the plugin's own state for *session_id*.

    Returns ``None`` when the plugin never wrote a file for this session, which
    is different from "it is off": one means unknown, the other means known-off.
    Collapsing them would let a promotion that silently did nothing look like a
    deliberate refusal.
    """
    path = state_dir() / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.debug("modo-auto state unreadable for %s: %s", session_id, exc)
        return None
    return bool(payload.get("on"))


def counts_as_question(text: str) -> bool:
    """Whether an assistant turn looks like it stopped to ask the owner."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(hint in lowered for hint in _QUESTION_HINTS)


@dataclass(frozen=True, slots=True)
class PromotionVerdict:
    """Why a session should (or should not) be handed autonomy."""

    should_promote: bool
    reason: str
    questions: int = 0


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """Decides when asking-in-a-loop justifies turning autonomy on."""

    question_threshold: int = DEFAULT_QUESTION_THRESHOLD

    def evaluate(
        self,
        session_id: str,
        recent_assistant_turns: Sequence[str],
        *,
        autonomous: Optional[bool] = None,
    ) -> PromotionVerdict:
        if autonomous is True:
            return PromotionVerdict(False, "already autonomous")

        questions = sum(1 for t in recent_assistant_turns if counts_as_question(t))
        if questions < self.question_threshold:
            return PromotionVerdict(
                False, f"only {questions} question(s) in recent turns", questions
            )

        return PromotionVerdict(
            True, f"stuck asking ({questions} questions)", questions
        )


def confirm_promotion(session_id: str) -> bool:
    """Verify the switch is really on, from the plugin's state -- not the reply.

    An agent can answer "done" without having called the tool, so the reply is
    not evidence. Unknown state is treated as not promoted, so a promotion that
    quietly failed is retried rather than assumed.
    """
    return is_autonomous(session_id) is True


_ACK_RE = re.compile(r"\bmodo[_\s-]?auto\b.*\b(on|ligad)", re.IGNORECASE)


def reply_claims_success(reply: str) -> bool:
    """Whether the session *claims* it enabled autonomy.

    Useful for diagnostics -- a session that claims success while the state says
    otherwise is a different problem from one that never answered -- but never
    as the confirmation itself.
    """
    return bool(_ACK_RE.search(reply or ""))


__all__ = [
    "DEFAULT_QUESTION_THRESHOLD",
    "PROMOTION_PROMPT",
    "PromotionPolicy",
    "PromotionVerdict",
    "confirm_promotion",
    "counts_as_question",
    "is_autonomous",
    "reply_claims_success",
    "state_dir",
]
