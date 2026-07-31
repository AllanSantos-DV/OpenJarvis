"""The product, end to end: the owner asks, Jarvis builds, Jarvis reports.

Every piece has been proven alone -- the brain answers, the arms open a session,
the shadow blocks, the memory records. None of that is the product. The product
is the one sentence the owner said:

    "quando eu falar pro Jarvis construir um produto, em vez de ele mesmo
     colocar um subagente pra fazer isso, ele vai abrir uma sessao dentro do
     GitHub Copilot IDE, mandar o prompt pro agente da sessao, e o agente da
     sessao faz a codificacao. E aí pode acompanhar o trabalho pedindo o status
     da sessao, lendo o transcript."

So this exercises exactly that chain, with real sessions in the real store:
    ask -> decide it is build work -> open a session -> hand over the job
        -> follow it -> report back.

Opt-in, because it spends real quota on real sessions.
"""

from __future__ import annotations

import os

import pytest

from openjarvis.tools.copilot_ide import CopilotIdeTool


def _require_real_cli() -> None:
    from openjarvis.agents.copilot_cli import is_copilot_cli_available

    if os.environ.get("RUN_COPILOT_CONTRACT") != "1":
        pytest.skip("set RUN_COPILOT_CONTRACT=1 to spend real Copilot quota")
    if not is_copilot_cli_available():
        pytest.skip("Copilot CLI binary is not on PATH")

    from openjarvis.conductor.runner import _cli_authenticates

    if not _cli_authenticates():
        pytest.skip("the Copilot CLI cannot authenticate here (run `copilot login`)")


@pytest.mark.contract
def test_jarvis_delegates_a_build_and_follows_it(tmp_path):
    """The whole product in one test, on the owner's real machine.

    What would break silently without it: any of the four steps can regress on
    its own and every unit test stays green, because each is proven in
    isolation. The owner only ever sees the chain.
    """
    _require_real_cli()

    tool = CopilotIdeTool()
    workspace = tmp_path / "projeto"
    workspace.mkdir()

    # 1. The owner asks for something to be BUILT -- Jarvis does not write it
    #    itself, it opens a session in the IDE and hands the job over.
    opened = tool.execute(
        action="open",
        prompt=(
            "Crie um arquivo chamado ola.txt com exatamente uma linha: "
            "Jarvis esteve aqui. Depois responda somente: FEITO."
        ),
        cwd=str(workspace),
    )
    assert opened.success, opened.content
    session_id = opened.metadata["session_id"]

    # 2. The work actually landed. This is the difference between "the agent
    #    said it did" and "it did" -- the failure this project has paid for
    #    more than once.
    created = workspace / "ola.txt"
    assert created.exists(), (
        f"a sessao respondeu mas o arquivo nao existe. Resposta: {opened.content}"
    )
    assert "Jarvis esteve aqui" in created.read_text(encoding="utf-8")

    # 3. Jarvis follows the work: same session, more instruction.
    followed = tool.execute(
        action="send",
        session_id=session_id,
        prompt="Acrescente uma segunda linha: Segunda passagem. Responda: FEITO.",
        cwd=str(workspace),
    )
    assert followed.success, followed.content
    assert "Segunda passagem" in created.read_text(encoding="utf-8")

    # 4. And can report on it -- reading the session's own record, not its word.
    status = tool.execute(action="status", session_id=session_id, turns=5)
    assert status.success, status.content
    assert session_id in str(status.content)
    turns = status.metadata.get("recent_turns") or []
    assert len(turns) >= 2, f"esperava 2 turnos registrados, vi {len(turns)}"


@pytest.mark.contract
def test_jarvis_decides_and_delegates_a_real_build(tmp_path):
    """The owner's sentence, whole, through the brain -- not around it.

    The earlier E2E called the arms directly, which proves the arms and skips
    the part that makes it a product: Jarvis DECIDING this is work rather than
    conversation. That decision is the difference between an assistant and a
    tool with a nice name.
    """
    _require_real_cli()

    from openjarvis.conductor.brain import JarvisBrain

    workspace = tmp_path / "projeto"
    workspace.mkdir()
    brain = JarvisBrain(workspace=str(workspace))

    # A question is answered. No session, no files.
    chat = brain.handle("Em uma linha: o que e um arquivo de log?")
    assert chat.kind == "conversa", chat.answer
    assert chat.session_id == ""
    assert not any(workspace.iterdir()), "uma conversa nao deveria criar nada"

    # Work is handed to a session, which does it.
    build = brain.handle(
        "Crie um arquivo notas.txt com exatamente uma linha: decidido pelo Jarvis."
    )
    assert build.delegated, f"o Jarvis tratou como conversa: {build.answer}"
    assert build.session_id, build.answer

    created = workspace / "notas.txt"
    assert created.exists(), f"a sessao respondeu mas nada foi criado: {build.answer}"
    assert "decidido pelo Jarvis" in created.read_text(encoding="utf-8")

    # And he can report on it from the session's own record.
    report = brain.follow(build.session_id)
    assert build.session_id in report


@pytest.mark.contract
def test_a_second_instruction_does_not_open_a_second_session(tmp_path):
    """The silent failure that cost a day, guarded at the product level.

    A prompt that lands in a NEW session leaves the owner watching one where
    nothing ever arrives, while every call reports success.
    """
    _require_real_cli()

    tool = CopilotIdeTool()
    workspace = tmp_path / "projeto"
    workspace.mkdir()

    opened = tool.execute(
        action="open", prompt="Responda somente: um.", cwd=str(workspace)
    )
    assert opened.success, opened.content
    session_id = opened.metadata["session_id"]

    followed = tool.execute(
        action="send",
        session_id=session_id,
        prompt="Responda somente: dois.",
        cwd=str(workspace),
    )

    assert followed.success, followed.content
    assert followed.metadata["session_id"] == session_id
