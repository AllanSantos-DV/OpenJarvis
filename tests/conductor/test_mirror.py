"""Mirror and Shadow.

The property worth pinning down is not that the mirror finds patterns -- it is
that the shadow can stop one. An assistant that learns to imitate its owner
learns to agree with him first, and a critic that fails open would let exactly
that through.
"""

from __future__ import annotations

import json

import pytest

from openjarvis.conductor.mirror import MirrorShadow, _as_dialogue, _parse_json


class _Answer:
    def __init__(self, content, error=False):
        self.content = content
        self.metadata = {"error": True} if error else {}


class _Agent:
    """Replays scripted answers: first the mirror's, then the shadow's."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else _Answer("{}")


class _Result:
    def __init__(self, content, success=True):
        self.content = content
        self.success = success


class _Reader:
    def __init__(self, sessions, turns_by_session):
        self._sessions = sessions
        self._turns = turns_by_session

    def execute(self, **params):
        if params.get("action") == "list":
            return _Result({"sessions": self._sessions})
        sid = params.get("session_id")
        if sid not in self._turns:
            return _Result(None, success=False)
        return _Result({"recent_turns": self._turns[sid]})


class _Memory:
    def __init__(self):
        self.stored = []

    def store(self, content, metadata=None):
        self.stored.append((content, metadata or {}))


def _reader():
    return _Reader(
        [{"session_id": "s1"}],
        {
            "s1": [
                {"user_message": "faz direito", "assistant_response": "ok"},
                {"user_message": "mediu ou achou?", "assistant_response": "medi"},
            ]
        },
    )


def _mirror_says(*patterns):
    return _Answer(json.dumps({"padroes": list(patterns)}))


def _shadow_says(*verdicts):
    return _Answer(json.dumps({"vereditos": list(verdicts)}))


PATTERN = {
    "padrao": "exige medicao antes de aceitar uma afirmacao",
    "evidencia": ["mediu ou achou?", "faz direito"],
    "confianca": 0.8,
}


def test_an_accepted_pattern_survives():
    agent = _Agent(
        _mirror_says(PATTERN),
        _shadow_says({"padrao": PATTERN["padrao"], "aceito": True, "motivo": "real"}),
    )
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
    ).reflect()

    assert [p.text for p in result.accepted] == [PATTERN["padrao"]]
    assert result.rejected == []


def test_the_shadow_can_reject_a_pattern():
    agent = _Agent(
        _mirror_says(PATTERN),
        _shadow_says(
            {"padrao": PATTERN["padrao"], "aceito": False, "motivo": "bajulacao"}
        ),
    )
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
    ).reflect()

    assert result.accepted == []
    assert result.rejected[0].verdict_reason == "bajulacao"


def test_an_unreadable_shadow_rejects_everything():
    """Failing open here would be the whole bug.

    If the critic cannot be read and we kept the patterns anyway, the one thing
    that reaches the persona unreviewed is exactly what the critic exists to
    catch: Jarvis learning to agree.
    """
    agent = _Agent(_mirror_says(PATTERN), _Answer("desculpe, nao consegui"))
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
    ).reflect()

    assert result.accepted == []
    assert len(result.rejected) == 1


def test_a_pattern_the_shadow_ignored_is_not_accepted():
    agent = _Agent(
        _mirror_says(PATTERN),
        _shadow_says({"padrao": "outro padrao qualquer", "aceito": True}),
    )
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
    ).reflect()

    assert result.accepted == []
    assert "nao avaliou" in result.rejected[0].verdict_reason


def test_a_pattern_seen_once_is_an_anecdote():
    thin = {"padrao": "gosta de python", "evidencia": ["usei python"]}
    agent = _Agent(_mirror_says(thin))
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
    ).reflect()

    assert result.accepted == [] and result.rejected == []
    # The shadow is never even consulted -- nothing survived the floor.
    assert len(agent.prompts) == 1


def test_both_verdicts_are_remembered():
    """Rejections are worth as much as acceptances: they stop a re-learn."""
    memory = _Memory()
    agent = _Agent(
        _mirror_says(PATTERN),
        _shadow_says(
            {"padrao": PATTERN["padrao"], "aceito": False, "motivo": "generalizou"}
        ),
    )
    MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=memory
    ).reflect()

    (content, metadata), = memory.stored
    assert "REJEITADO" in content
    assert metadata["accepted"] is False


def test_a_memory_that_fails_does_not_kill_the_pass():
    class _Broken:
        def store(self, *a, **k):
            raise RuntimeError("memoria fora do ar")

    agent = _Agent(
        _mirror_says(PATTERN),
        _shadow_says({"padrao": PATTERN["padrao"], "aceito": True}),
    )
    result = MirrorShadow(
        agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Broken()
    ).reflect()

    assert len(result.accepted) == 1


def test_no_sessions_is_reported_not_silently_empty():
    result = MirrorShadow(
        agent_factory=lambda: _Agent(),
        sessions_reader=_Reader([], {}),
        memory=_Memory(),
    ).reflect()

    assert not result.ok
    assert "nenhuma sessao" in result.error


def test_json_survives_a_chatty_model():
    payload = _parse_json(
        'Claro! Aqui esta:\n```json\n{"padroes":[]}\n```\nEspero ajudar'
    )
    assert payload == {"padroes": []}


def test_no_json_at_all_is_none_not_empty():
    # The caller must be able to tell "said nothing usable" from "said none".
    assert _parse_json("nao consegui responder") is None


def test_dialogue_labels_who_spoke():
    text = _as_dialogue([{"user_message": "faz", "assistant_response": "feito"}])
    assert "[DONO] faz" in text and "[AGENTE] feito" in text


def test_an_agent_error_is_raised_not_swallowed():
    agent = _Agent(_Answer("quota estourada", error=True))
    with pytest.raises(RuntimeError, match="quota"):
        MirrorShadow(
            agent_factory=lambda: agent, sessions_reader=_reader(), memory=_Memory()
        ).reflect()
