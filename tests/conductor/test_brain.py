"""The router: does this get answered, or built?

The property that matters is not that it classifies well -- a model does that.
It is that a wrong answer is never expensive in the dangerous direction: opening
a session in the owner's project because he asked a question is worse than
answering a build request with words, because the first writes files.
"""

from __future__ import annotations

from openjarvis.conductor.brain import JarvisBrain


class _Answer:
    def __init__(self, content, error=False):
        self.content = content
        self.metadata = {"error": True} if error else {}


class _Agent:
    """Replays scripted answers: the classification, then the reply."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else _Answer("")


class _Result:
    def __init__(self, content, success=True, metadata=None):
        self.content = content
        self.success = success
        self.metadata = metadata or {}


class _Ide:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or _Result("feito", metadata={"session_id": "s-1"})

    def execute(self, **params):
        self.calls.append(params)
        return self._result


def test_a_question_is_answered_not_built():
    agent = _Agent(_Answer("CONVERSA"), _Answer("Um logger e um objeto que..."))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle(
        "me explica o que e um logger"
    )

    assert decision.kind == "conversa"
    assert "logger e um objeto" in decision.answer
    assert ide.calls == []  # nothing was opened


def test_a_build_request_is_delegated_to_a_session():
    agent = _Agent(_Answer("TRABALHO"))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle(
        "cria um logger estruturado no projeto"
    )

    assert decision.delegated
    assert decision.session_id == "s-1"
    (call,) = ide.calls
    assert call["action"] == "open"
    assert call["cwd"] == "C:/projeto"
    # The owner's own words go to the session, not a summary of them.
    assert call["prompt"] == "cria um logger estruturado no projeto"


def test_an_explicit_directory_wins_over_the_default():
    agent = _Agent(_Answer("TRABALHO"))
    ide = _Ide()

    JarvisBrain(agent=agent, ide=ide, workspace="C:/padrao").handle(
        "arruma o bug", cwd="C:/outro"
    )

    assert ide.calls[0]["cwd"] == "C:/outro"


def test_work_with_nowhere_to_put_it_asks_instead_of_guessing():
    """Guessing a directory means the work lands where nobody looks."""
    agent = _Agent(_Answer("TRABALHO"))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide).handle("cria um logger")

    assert ide.calls == []
    assert "qual projeto" in decision.answer or "pasta" in decision.answer


def test_an_unreadable_classification_falls_back_to_conversation():
    """The safe direction. Answering a build request with words costs a
    sentence; opening a session nobody asked for writes files.
    """
    agent = _Agent(_Answer("sei la, talvez"), _Answer("aqui esta"))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle(
        "faz alguma coisa"
    )

    assert decision.kind == "conversa"
    assert ide.calls == []


def test_a_broken_classifier_does_not_break_the_conversation():
    agent = _Agent(_Answer("quota estourada", error=True), _Answer("oi"))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle("oi")

    assert decision.kind == "conversa"
    assert ide.calls == []


def test_a_verbose_classification_is_still_understood():
    """The model was asked for one word and sometimes writes a sentence."""
    agent = _Agent(_Answer("Isso e um pedido de TRABALHO em codigo."))
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle(
        "implementa o cache"
    )

    assert decision.delegated


def test_a_failed_delegation_reports_the_failure():
    """Not silently downgraded to a chat reply -- the owner asked for work."""
    agent = _Agent(_Answer("TRABALHO"))
    ide = _Ide(_Result("diretorio inexistente", success=False))

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/nao-existe").handle(
        "cria o modulo"
    )

    assert decision.kind == "trabalho"
    assert decision.session_id == ""
    assert "inexistente" in decision.answer


def test_following_reads_the_sessions_own_record():
    agent = _Agent()
    ide = _Ide(_Result("Sessao s-1 - 2 turnos"))

    report = JarvisBrain(agent=agent, ide=ide).follow("s-1")

    assert "s-1" in report
    assert ide.calls[0]["action"] == "status"


def test_silence_is_not_a_build_request():
    agent = _Agent()
    ide = _Ide()

    decision = JarvisBrain(agent=agent, ide=ide, workspace="C:/projeto").handle("   ")

    assert decision.kind == "conversa"
    assert agent.prompts == []  # not even worth a classification turn
