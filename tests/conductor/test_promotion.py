"""Promoting a stuck session to autonomy, and proving it actually happened."""

from __future__ import annotations

import json

import pytest

from openjarvis.conductor.promotion import (
    PROMOTION_PROMPT,
    PromotionPolicy,
    confirm_promotion,
    counts_as_question,
    is_autonomous,
    reply_claims_success,
)


@pytest.fixture
def toggles(tmp_path, monkeypatch):
    """Stand in for the modo-auto plugin's own state directory."""
    monkeypatch.setenv("COPILOT_MODO_AUTO_DIR", str(tmp_path))

    def write(session_id: str, on: bool) -> None:
        (tmp_path / f"{session_id}.json").write_text(
            json.dumps({"on": on, "ts": 1}), encoding="utf-8"
        )

    return write


def test_prompt_is_a_single_line():
    """Newlines make --resume open a new session instead of continuing this one."""
    assert "\n" not in PROMOTION_PROMPT


def test_prompt_names_the_tool_and_the_action():
    assert "modo_auto" in PROMOTION_PROMPT
    assert "on" in PROMOTION_PROMPT


def test_unknown_state_is_not_the_same_as_off(toggles):
    """A promotion that silently did nothing must not look like a refusal."""
    assert is_autonomous("never-seen") is None

    toggles("known-off", False)
    assert is_autonomous("known-off") is False


def test_reads_the_plugins_own_state(toggles):
    toggles("s1", True)

    assert is_autonomous("s1") is True
    assert confirm_promotion("s1") is True


def test_corrupt_state_file_is_treated_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_MODO_AUTO_DIR", str(tmp_path))
    (tmp_path / "s1.json").write_text("{not json", encoding="utf-8")

    assert is_autonomous("s1") is None
    assert confirm_promotion("s1") is False


def test_confirmation_ignores_what_the_session_claims(toggles):
    """An agent can answer 'done' without having called the tool."""
    toggles("s1", False)

    assert reply_claims_success("Pronto, modo_auto ligado!") is True
    assert confirm_promotion("s1") is False


def test_one_question_is_not_stuck(toggles):
    """Asking once is a session doing its job, not a session to take over."""
    verdict = PromotionPolicy().evaluate("s1", ["Posso prosseguir com a fase 2?"])

    assert verdict.should_promote is False
    assert verdict.questions == 1


def test_repeated_questions_justify_promotion():
    turns = [
        "Posso prosseguir com a fase 2?",
        "Confirma que devo usar o backend antigo?",
        "Quer que eu rode os testes agora?",
    ]

    verdict = PromotionPolicy().evaluate("s1", turns)

    assert verdict.should_promote is True
    assert verdict.questions == 3
    assert "stuck asking" in verdict.reason


def test_already_autonomous_session_is_left_alone():
    turns = ["Posso prosseguir?", "Confirma?"]

    verdict = PromotionPolicy().evaluate("s1", turns, autonomous=True)

    assert verdict.should_promote is False
    assert verdict.reason == "already autonomous"


def test_working_turns_do_not_count_as_questions():
    turns = ["Implementei a fase 2 e os testes passaram.", "Segui o plano."]

    assert PromotionPolicy().evaluate("s1", turns).should_promote is False


def test_question_detection_covers_the_common_phrasings():
    assert counts_as_question("Qual das opcoes voce prefere") is True
    assert counts_as_question("Devo seguir com a alternativa B") is True
    assert counts_as_question("Terminei tudo.") is False
    assert counts_as_question("") is False
