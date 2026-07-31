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
