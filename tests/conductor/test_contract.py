"""Contract probes for external binaries.

These are opt-in because they spend real Copilot quota, but when enabled they
fail loudly if the CLI footer stops exposing the fields our parser relies on.
"""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from openjarvis.agents.copilot_cli import CopilotCliAgent, is_copilot_cli_available

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

_SESSION_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
    branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL, user_message TEXT, assistant_response TEXT,
    timestamp TEXT
);
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    checkpoint_number INTEGER NOT NULL, title TEXT, overview TEXT, history TEXT,
    work_done TEXT, technical_details TEXT, important_files TEXT,
    next_steps TEXT, created_at TEXT
);
CREATE TABLE session_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    file_path TEXT NOT NULL, tool_name TEXT, turn_index INTEGER,
    first_seen_at TEXT
);
"""


@pytest.mark.contract
def test_copilot_cli_output_contract_with_disposable_profile(tmp_path):
    _require_real_cli()

    profile = tmp_path / "profile"
    store = profile / ".copilot" / "session-store.db"
    store.parent.mkdir(parents=True)
    conn = sqlite3.connect(store)
    conn.executescript(_SESSION_SCHEMA)
    conn.commit()
    conn.close()

    agent = CopilotCliAgent(
        None,
        "auto",
        workspace=str(tmp_path),
        timeout=120,
        env={
            "USERPROFILE": str(profile),
            "HOME": str(profile),
            "COPILOT_SESSION_STORE": str(store),
            "VOICE_SUMMARY_MIN_CHARS": "1000000",
        },
    )

    result = agent.run("Responda somente: contrato-ok")

    assert result.metadata.get("error") is not True, result.content
    assert "contrato-ok" in result.content.lower()
    assert _UUID_RE.match(result.metadata.get("session_id", ""))
    footer = result.metadata.get("cli_footer", {})
    assert "AI Credits" in footer or "Tokens" in footer


@pytest.mark.contract
def test_resume_appends_to_the_same_session(tmp_path):
    """The invariant the whole conductor rests on: --resume must APPEND.

    A resume that silently starts a fresh session instead of appending is the
    exact failure that cost a day of debugging -- the read-back was right, there
    was no new turn in the target session, because the CLI had opened another
    one. Nothing in the code reveals why; only exercising it does.

    Opt-in like the probe above, since it spends real quota, but this is the
    canonical proof: a new turn lands in the SAME session id, and no extra
    session appears in the store.
    """
    _require_real_cli()

    profile = tmp_path / "profile"
    (profile / ".copilot").mkdir(parents=True)
    # No hand-written schema and no COPILOT_SESSION_STORE: let the CLI create
    # its own store. Measured -- the schema it builds has 17 tables, and the
    # four-table stand-in this test used to impose left the turn unwritten,
    # which read exactly like the resume bug it was meant to detect.
    store = profile / ".copilot" / "session-store.db"
    env = {
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "VOICE_SUMMARY_MIN_CHARS": "1000000",
    }

    first = CopilotCliAgent(
        None, "auto", workspace=str(tmp_path), timeout=180, env=env
    ).run("Responda somente: um")
    assert first.metadata.get("error") is not True, first.content
    session_id = first.metadata.get("session_id", "")
    assert _UUID_RE.match(session_id)

    # Same delay applies to the FIRST turn: without waiting for it, `before`
    # reads 0 and the comparison below proves nothing.
    assert _turn_lands(store, session_id, 0), "the first turn never landed"

    sessions_before = _session_ids(store)
    turns_before = _turn_count(store, session_id)

    resumed = CopilotCliAgent(
        None,
        "auto",
        workspace=str(tmp_path),
        timeout=180,
        env=env,
        session_id=session_id,
        sandboxed=False,
    ).run("Responda somente: dois")

    assert resumed.metadata.get("error") is not True, resumed.content
    # The app persists the turn shortly AFTER the CLI process exits, so reading
    # once is a false failure -- measured, and the source of a day spent chasing
    # "no new turn recorded". Poll instead.
    assert _turn_lands(store, session_id, turns_before), (
        "resume produced no new turn in the target session"
    )
    assert _session_ids(store) == sessions_before, (
        "resume created a new session instead of appending to the target"
    )


def _turn_lands(store, session_id: str, before: int, timeout: float = 60.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _turn_count(store, session_id) > before:
            return True
        time.sleep(1.0)
    return False


@pytest.mark.contract
def test_a_headless_session_can_actually_invoke_an_mcp_tool(tmp_path):
    """Listing a tool is not having it -- the third time this bill came due.

    Twice already the lesson was written down: to prove a permission holds, the
    test must EXERCISE it. Twice it was proved by hand and not by code, and the
    second time the guarantee shipped FALSE -- the session listed all seven
    ata-ao-vivo tools and then died on the first call with

        Permission denied and could not request permission from user

    A headless child runs with --no-ask-user and has nobody to ask, so
    --additional-mcp-config alone grants tools the agent can see, plan around,
    and never use. Nothing structural stopped a third repeat; this is that
    something.

    Opt-in like the other contract probes -- it spends real quota and needs the
    owner's own bridge config -- but it fails LOUDLY when the permission model
    changes, instead of waiting for a session to quietly do less than asked.
    """
    _require_real_cli()

    from openjarvis.tools.mcp_bridge_config import build_config

    servers = build_config()["mcpServers"]
    if "ata-ao-vivo" not in servers:
        pytest.skip("this machine's bridge does not carry ata-ao-vivo")

    from openjarvis.tools.copilot_ide import CopilotIdeTool

    result = CopilotIdeTool().execute(
        action="open",
        prompt=(
            "CHAME a ferramenta ata_estado (sem argumentos) e responda em UMA "
            "linha comecando com RESULTADO: seguido do que ela devolveu. Se a "
            "chamada falhar por qualquer motivo, responda FALHOU: <erro>."
        ),
        cwd=str(tmp_path),
    )

    assert result.success, result.content
    answer = str(result.content)
    assert "FALHOU" not in answer, answer
    # The daemon always answers with an `aviso` or an `itens` count; either one
    # is proof the call reached it and came back.
    assert "aviso" in answer or "itens" in answer, answer


def _require_real_cli() -> None:
    """Skip unless this machine can genuinely exercise the CLI.

    Deliberately does NOT demand GH_TOKEN/GITHUB_TOKEN. The owner authenticates
    through `copilot login`, whose credential lives in the system store -- and
    a shell started from a shortcut never has that variable, because the app
    injects it into its own process. Demanding it skipped every run on a machine
    that works, which turns a gate into decoration.
    """
    if os.environ.get("RUN_COPILOT_CONTRACT") != "1":
        pytest.skip("set RUN_COPILOT_CONTRACT=1 to spend real Copilot quota")
    if not is_copilot_cli_available():
        pytest.skip("Copilot CLI binary is not on PATH")

    from openjarvis.conductor.runner import _cli_authenticates

    if not _cli_authenticates():
        pytest.skip("the Copilot CLI cannot authenticate here (run `copilot login`)")


def _fresh_store(profile):
    store = profile / ".copilot" / "session-store.db"
    store.parent.mkdir(parents=True)
    conn = sqlite3.connect(store)
    conn.executescript(_SESSION_SCHEMA)
    conn.commit()
    conn.close()
    return store


def _session_ids(store) -> set:
    conn = sqlite3.connect(str(store))
    try:
        return {row[0] for row in conn.execute("SELECT id FROM sessions")}
    finally:
        conn.close()


def _turn_count(store, session_id: str) -> int:
    conn = sqlite3.connect(str(store))
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(count)
    finally:
        conn.close()

