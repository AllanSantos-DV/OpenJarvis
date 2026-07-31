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
    if os.environ.get("RUN_COPILOT_CONTRACT") != "1":
        pytest.skip("set RUN_COPILOT_CONTRACT=1 to spend real Copilot quota")
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        pytest.skip("Copilot CLI contract requires GH_TOKEN or GITHUB_TOKEN")
    if not is_copilot_cli_available():
        pytest.skip("Copilot CLI binary is not on PATH")

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
    store = _fresh_store(profile)
    env = {
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "COPILOT_SESSION_STORE": str(store),
        "VOICE_SUMMARY_MIN_CHARS": "1000000",
    }

    first = CopilotCliAgent(
        None, "auto", workspace=str(tmp_path), timeout=180, env=env
    ).run("Responda somente: um")
    assert first.metadata.get("error") is not True, first.content
    session_id = first.metadata.get("session_id", "")
    assert _UUID_RE.match(session_id)

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
    assert _turn_count(store, session_id) > turns_before, (
        "resume produced no new turn in the target session"
    )
    assert _session_ids(store) == sessions_before, (
        "resume created a new session instead of appending to the target"
    )


def _require_real_cli() -> None:
    if os.environ.get("RUN_COPILOT_CONTRACT") != "1":
        pytest.skip("set RUN_COPILOT_CONTRACT=1 to spend real Copilot quota")
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        pytest.skip("Copilot CLI contract requires GH_TOKEN or GITHUB_TOKEN")
    if not is_copilot_cli_available():
        pytest.skip("Copilot CLI binary is not on PATH")


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

