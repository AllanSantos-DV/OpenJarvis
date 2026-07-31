"""Mirror and Shadow: the pair that keeps Jarvis useful instead of agreeable.

**Mirror** reads what actually happened -- the transcripts of the owner's IDE
sessions -- and builds a picture of how he works: what he asks for, how he
phrases it, what he corrects, what he never wants. Not facts about him, habits
of his: the difference between "prefers Python" and "rejects an answer that
claims something works without showing the measurement".

**Shadow** exists because a mirror alone is dangerous. An assistant that learns
to imitate its owner learns, first and fastest, to agree with him -- the same
failure that makes assistants tell people what they want to hear. Shadow reads
what Mirror concluded and pushes back: which of these are real patterns, and
which are just Jarvis learning to nod?

Only what survives Shadow is kept. A pattern Shadow rejects is written down as
rejected, with the reason, so the same wrong conclusion is not re-learnt next
week.

Both run on the owner's Copilot subscription, like everything else here. Neither
touches the GPU.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: How many recent turns of a session the mirror reads. Enough to see a working
#: rhythm, small enough that a resume stays affordable.
DEFAULT_TURNS = 12

#: A pattern seen once is an anecdote. This is the floor for proposing one.
MIN_EVIDENCE = 2

_MIRROR_PROMPT = (
    "Voce e o ESPELHO do Jarvis. Leia os trechos de conversa abaixo, entre o "
    "dono e os agentes das sessoes dele, e extraia PADROES DE COMPORTAMENTO "
    "do DONO -- nao fatos sobre ele. Padrao e o que se repete: como ele pede, "
    "o que ele corrige, o que ele rejeita, o que ele valoriza. Ignore o "
    "assunto tecnico; foque em COMO ele trabalha. Responda SOMENTE um JSON "
    'no formato {"padroes":[{"padrao":"...","evidencia":["...","..."],'
    '"confianca":0.0}]} -- sem texto antes ou depois. Cada padrao precisa de '
    "pelo menos duas evidencias citadas literalmente do material. Se nao "
    'houver padrao com duas evidencias, devolva {"padroes":[]}. TRECHOS: '
)

_SHADOW_PROMPT = (
    "Voce e o SOMBRA do Jarvis, o contrapeso do espelho. O espelho tende a "
    "aprender a CONCORDAR com o dono, que e o pior defeito que um assistente "
    "pode ter. Avalie cada padrao proposto e decida: ACEITAR (padrao real, "
    "com evidencia que sustenta) ou REJEITAR (bajulacao, generalizacao de um "
    "caso unico, evidencia que nao sustenta a conclusao, ou padrao que "
    "tornaria o Jarvis mais concordante). Seja duro: na duvida, rejeite. "
    "Responda SOMENTE um JSON no formato "
    '{"vereditos":[{"padrao":"...","aceito":true,"motivo":"..."}]} -- sem '
    "texto antes ou depois. PADROES PROPOSTOS: "
)

_SESSION_SHADOW_PROMPT = (
    "Voce e o SOMBRA do Jarvis. O Jarvis vai mandar um turno para a sessao "
    "descrita abaixo AGORA, SEM ninguem olhando. Sua unica pergunta e: e "
    "seguro deixar? REPROVE quando: a sessao fez uma PERGUNTA DIRETA ao dono "
    "(decisao dele, nao do agente); esta no meio de algo destrutivo ou "
    "irreversivel (migracao, deploy, apagar dados, mexer em producao, "
    "credenciais, chaves); parece travada repetindo o mesmo erro (responder "
    "so aprofunda o buraco); ou o contexto e ambiguo demais para agir sem o "
    "dono. APROVE quando for continuacao de trabalho de rotina com proximo "
    "passo claro. Na duvida, REPROVE: uma resposta adiada custa minutos, uma "
    "resposta errada em sessao real custa o trabalho. Responda SOMENTE um "
    'JSON no formato {"aprovado":true,"motivo":"..."} -- sem texto antes ou '
    "depois. O motivo deve ser uma frase curta em portugues. SESSAO: "
)


@dataclass
class Pattern:
    """One observed way the owner works."""

    text: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0
    accepted: Optional[bool] = None
    verdict_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "padrao": self.text,
            "evidencia": self.evidence,
            "confianca": self.confidence,
            "aceito": self.accepted,
            "motivo": self.verdict_reason,
        }


@dataclass
class ReflectionResult:
    """What one mirror+shadow pass concluded."""

    accepted: List[Pattern] = field(default_factory=list)
    rejected: List[Pattern] = field(default_factory=list)
    sessions_read: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"espelho falhou: {self.error}"
        return (
            f"{self.sessions_read} sessoes lidas, "
            f"{len(self.accepted)} padroes aceitos, "
            f"{len(self.rejected)} rejeitados pelo sombra"
        )


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of a model answer.

    Models wrap JSON in prose and fences even when told not to. Rather than
    failing the whole pass on presentation, find the outermost object. Returns
    None when there is genuinely no object -- a caller must be able to tell
    "the model said nothing usable" from "the model said no patterns".
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.debug("mirror/shadow answer was not valid JSON: %s", text[:200])
        return None


class MirrorShadow:
    """Learn how the owner works, with a critic that refuses easy conclusions."""

    def __init__(
        self,
        *,
        agent_factory: Optional[Any] = None,
        sessions_reader: Optional[Any] = None,
        memory: Optional[Any] = None,
        turns: int = DEFAULT_TURNS,
    ) -> None:
        self._agent_factory = agent_factory
        self._reader = sessions_reader
        self._memory = memory
        self._turns = turns

    # -- the pass ------------------------------------------------------------

    def reflect(self, *, limit: int = 5) -> ReflectionResult:
        """Read recent sessions, propose patterns, and let Shadow judge them."""
        excerpts = self._collect(limit=limit)
        if not excerpts:
            return ReflectionResult(error="nenhuma sessao com conversa para ler")

        proposed = self._mirror(excerpts)
        if not proposed:
            return ReflectionResult(sessions_read=len(excerpts))

        judged = self._shadow(proposed)
        result = ReflectionResult(sessions_read=len(excerpts))
        for pattern in judged:
            (result.accepted if pattern.accepted else result.rejected).append(pattern)

        self._remember(result)
        return result

    # -- steps ---------------------------------------------------------------

    def _collect(self, *, limit: int) -> List[str]:
        """Recent owner/assistant exchanges, one blob per session."""
        reader = self._reader or self._default_reader()
        listing = reader.execute(action="list", limit=limit)
        if not listing.success or not isinstance(listing.content, dict):
            return []

        excerpts: List[str] = []
        for session in listing.content.get("sessions", []):
            session_id = session.get("session_id") or session.get("id")
            if not session_id:
                continue
            detail = reader.execute(
                action="detail", session_id=session_id, turns=self._turns
            )
            if not detail.success or not isinstance(detail.content, dict):
                continue
            text = _as_dialogue(detail.content.get("recent_turns") or [])
            if text:
                excerpts.append(text)
        return excerpts

    def _mirror(self, excerpts: Sequence[str]) -> List[Pattern]:
        answer = self._ask(_MIRROR_PROMPT + " ".join(excerpts))
        payload = _parse_json(answer)
        if payload is None:
            return []

        patterns: List[Pattern] = []
        for raw in payload.get("padroes", []):
            evidence = [str(item) for item in (raw.get("evidencia") or [])]
            # A pattern seen once is an anecdote. Dropping these here means
            # Shadow spends its judgement on real candidates.
            if len(evidence) < MIN_EVIDENCE:
                continue
            text = str(raw.get("padrao") or "").strip()
            if not text:
                continue
            patterns.append(
                Pattern(
                    text=text,
                    evidence=evidence,
                    confidence=float(raw.get("confianca") or 0.0),
                )
            )
        return patterns

    def _shadow(self, proposed: Sequence[Pattern]) -> List[Pattern]:
        listing = json.dumps(
            [{"padrao": p.text, "evidencia": p.evidence} for p in proposed],
            ensure_ascii=False,
        )
        answer = self._ask(_SHADOW_PROMPT + listing)
        payload = _parse_json(answer)
        if payload is None:
            # Shadow could not be read. Rejecting everything is the safe
            # direction: an unreviewed pattern must never reach the persona,
            # because "learn to agree with him" is exactly what slips through
            # when the critic is skipped.
            for pattern in proposed:
                pattern.accepted = False
                pattern.verdict_reason = "sombra nao respondeu de forma legivel"
            return list(proposed)

        verdicts = {
            str(v.get("padrao") or "").strip(): v
            for v in payload.get("vereditos", [])
        }
        for pattern in proposed:
            verdict = verdicts.get(pattern.text)
            if verdict is None:
                pattern.accepted = False
                pattern.verdict_reason = "sombra nao avaliou este padrao"
                continue
            pattern.accepted = bool(verdict.get("aceito"))
            pattern.verdict_reason = str(verdict.get("motivo") or "")
        return list(proposed)

    def _remember(self, result: ReflectionResult) -> None:
        """Persist both verdicts.

        The rejections matter as much as the acceptances: written down, the same
        wrong conclusion is not re-learnt next week.
        """
        memory = self._memory or self._default_memory()
        if memory is None:
            return
        stamp = datetime.now(timezone.utc).isoformat()
        for pattern in result.accepted + result.rejected:
            verdict = "ACEITO" if pattern.accepted else "REJEITADO"
            try:
                memory.store(
                    f"[ESPELHO {verdict}] {pattern.text}\n"
                    f"Evidencia: {' | '.join(pattern.evidence)}\n"
                    f"Sombra: {pattern.verdict_reason}",
                    metadata={
                        "type": "mirror_pattern",
                        "accepted": bool(pattern.accepted),
                        "observed_at": stamp,
                    },
                )
            except Exception:  # noqa: BLE001 -- a lost note must not kill the pass
                logger.debug("could not store mirror pattern", exc_info=True)

    # -- seams ---------------------------------------------------------------

    def _ask(self, prompt: str) -> str:
        agent = (self._agent_factory or self._default_agent)()
        result = agent.run(prompt)
        if result.metadata.get("error"):
            raise RuntimeError(result.content)
        return result.content or ""

    def _default_agent(self):
        from openjarvis.agents.copilot_cli import CopilotCliAgent

        # Reflection is reading and judging, not doing: a fresh sandboxed
        # session with no tools is the right shape for it.
        return CopilotCliAgent(None, "auto", timeout=300, sandboxed=True)

    def _default_reader(self):
        from openjarvis.tools.copilot_sessions import CopilotSessionsTool

        return CopilotSessionsTool()

    def _default_memory(self):
        try:
            from openjarvis.tools.storage.native_java import NativeJavaMemoryBackend

            return NativeJavaMemoryBackend()
        except Exception:  # noqa: BLE001 -- memory is optional for a dry pass
            logger.debug("native-java memory unavailable", exc_info=True)
            return None


def _as_dialogue(turns: Sequence[Dict[str, Any]]) -> str:
    """Flatten turns into labelled dialogue the mirror can read."""
    lines: List[str] = []
    for turn in turns:
        asked = " ".join(str(turn.get("user_message") or "").split())
        answered = " ".join(str(turn.get("assistant_response") or "").split())
        if asked:
            lines.append(f"[DONO] {asked[:600]}")
        if answered:
            lines.append(f"[AGENTE] {answered[:400]}")
    return " ".join(lines)


@dataclass
class ShadowVerdict:
    """The shadow's answer on whether one session should be answered now."""

    approved: bool
    reason: str = ""


class SessionShadow:
    """The brake on the unattended loop: a second opinion before writing.

    ``MirrorShadow`` above learns how the owner works, over time. This is the
    other half and the urgent one: before the conductor sends a turn into a REAL
    session with nobody watching, something has to ask *should it?*

    It is deliberately narrow. It does not judge the work, it judges whether
    this is a moment to act unattended -- a session mid-migration, one that just
    asked the owner a direct question, one touching production. Those are the
    cases where a helpful answer is worse than silence.

    Blocking is cheap: the turn stays available and the owner sees it queued.
    Answering wrongly is not.
    """

    def __init__(
        self, *, agent_factory: Optional[Any] = None, timeout: int = 180
    ) -> None:
        self._agent_factory = agent_factory
        self._timeout = timeout

    def review(
        self,
        *,
        session_id: str,
        summary: str = "",
        cwd: str = "",
        last_turn: str = "",
        next_steps: str = "",
    ) -> ShadowVerdict:
        answer = self._ask(
            _SESSION_SHADOW_PROMPT
            + json.dumps(
                {
                    "resumo": summary[:400],
                    "pasta": cwd,
                    "ultima_resposta": last_turn[:800],
                    "proximos_passos": next_steps[:400],
                },
                ensure_ascii=False,
            )
        )
        payload = _parse_json(answer)
        if payload is None:
            # Unparseable is not permission. The reviewer failing silently is
            # indistinguishable from no reviewer at all.
            return ShadowVerdict(False, "sombra respondeu de forma ilegivel")
        approved = bool(payload.get("aprovado"))
        return ShadowVerdict(approved, str(payload.get("motivo") or ""))

    def _ask(self, prompt: str) -> str:
        agent = (self._agent_factory or self._default_agent)()
        result = agent.run(prompt)
        if result.metadata.get("error"):
            raise RuntimeError(result.content)
        return result.content or ""

    def _default_agent(self):
        from openjarvis.agents.copilot_cli import CopilotCliAgent
        from openjarvis.conductor.adapters import UNATTENDED_ENV

        # Judging, not doing: no tools, and the hook env a headless child needs.
        return CopilotCliAgent(
            None,
            "auto",
            timeout=self._timeout,
            sandboxed=True,
            env=dict(UNATTENDED_ENV),
        )


__all__ = [
    "MirrorShadow",
    "Pattern",
    "ReflectionResult",
    "SessionShadow",
    "ShadowVerdict",
]
