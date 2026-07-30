"""Unit tests for CopilotSessionsTool (uses a temporary store, not the real one)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.core.registry import ToolRegistry
from openjarvis.tools.copilot_sessions import CopilotSessionsTool, _parse_ts

_SCHEMA = """
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


@pytest.fixture(autouse=True)
def _register_copilot_sessions():
    """Re-register after any registry clear."""
    if not ToolRegistry.contains("copilot_sessions"):
        ToolRegistry.register_value("copilot_sessions", CopilotSessionsTool)


def _ago(minutes: float, *, iso: bool = True) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    if iso:
        return moment.isoformat().replace("+00:00", "Z")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def store(tmp_path):
    """A store with one fresh session, one long-idle one, and a checkpoint."""
    path = tmp_path / "session-store.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO sessions (id, cwd, summary)"
        " VALUES ('fresh', 'C:/a', 'Sessao ativa')"
    )
    conn.execute(
        "INSERT INTO turns"
        " (session_id, turn_index, user_message, assistant_response, timestamp)"
        " VALUES ('fresh', 0, 'oi', 'ola', ?)",
        (_ago(1),),
    )

    conn.execute(
        "INSERT INTO sessions (id, cwd, summary)"
        " VALUES ('stale', 'C:/b', 'Sessao parada')"
    )
    # SQLite-style timestamp, to prove both formats are handled.
    conn.execute(
        "INSERT INTO turns"
        " (session_id, turn_index, user_message, assistant_response, timestamp)"
        " VALUES ('stale', 0, 'faz isso', 'feito', ?)",
        (_ago(600, iso=False),),
    )
    conn.execute(
        "INSERT INTO checkpoints (session_id, checkpoint_number, title, overview,"
        " work_done, next_steps, created_at) VALUES"
        " ('stale', 1, 'Titulo', 'Visao geral', 'Fiz X', 'Falta revisar Y', ?)",
        (_ago(600, iso=False),),
    )
    conn.execute(
        "INSERT INTO session_files (session_id, file_path, first_seen_at)"
        " VALUES ('stale', 'src/app.py', ?)",
        (_ago(600, iso=False),),
    )

    conn.commit()
    conn.close()
    return path


def _tool(store):
    return CopilotSessionsTool(store_path=str(store))


def test_registered():
    assert ToolRegistry.contains("copilot_sessions")
    assert ToolRegistry.get("copilot_sessions") is CopilotSessionsTool


def test_parse_ts_accepts_both_store_formats():
    """The store mixes ISO 'Z' strings with SQLite's 'YYYY-MM-DD HH:MM:SS'."""
    assert _parse_ts("2026-07-30T20:13:27.930Z") is not None
    assert _parse_ts("2026-07-30 20:13:27") is not None
    assert _parse_ts("garbage") is None
    assert _parse_ts("") is None


def test_parse_ts_assumes_utc_for_naive_timestamps():
    parsed = _parse_ts("2026-07-30 20:13:27")
    assert parsed.tzinfo is timezone.utc


def test_list_returns_sessions_sorted_by_recency(store):
    result = _tool(store).execute(action="list")

    assert result.success
    ids = [s["session_id"] for s in result.content["sessions"]]
    assert ids == ["fresh", "stale"]
    assert result.content["count"] == 2


def test_list_computes_idle_minutes(store):
    sessions = _tool(store).execute(action="list").content["sessions"]

    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["fresh"]["idle_minutes"] < 5
    assert by_id["stale"]["idle_minutes"] > 500


def test_idle_filters_out_active_sessions(store):
    result = _tool(store).execute(action="idle", idle_minutes=60)

    ids = [s["session_id"] for s in result.content["sessions"]]
    assert ids == ["stale"]


def test_idle_scans_beyond_the_first_page(store, monkeypatch):
    """A long-idle session must not be hidden by noisy recent sessions."""
    result = _tool(store).execute(action="idle", idle_minutes=60, limit=1)

    assert [s["session_id"] for s in result.content["sessions"]] == ["stale"]


def test_idle_uses_default_threshold(store):
    result = _tool(store).execute(action="idle")

    assert [s["session_id"] for s in result.content["sessions"]] == ["stale"]


def test_detail_exposes_checkpoint_outcome(store):
    """The checkpoint is the app's own distilled result of the session."""
    result = _tool(store).execute(action="detail", session_id="stale")

    assert result.success
    detail = result.content
    assert detail["checkpoint"]["next_steps"] == "Falta revisar Y"
    assert detail["checkpoint"]["work_done"] == "Fiz X"
    assert detail["files_touched"] == ["src/app.py"]
    assert detail["recent_turns"][0]["user_message"] == "faz isso"


def test_detail_without_checkpoint_omits_the_key(store):
    detail = _tool(store).execute(action="detail", session_id="fresh").content

    assert "checkpoint" not in detail


def test_detail_requires_session_id(store):
    result = _tool(store).execute(action="detail")

    assert not result.success
    assert "requires a session_id" in result.content


def test_detail_of_unknown_session_fails(store):
    result = _tool(store).execute(action="detail", session_id="nope")

    assert not result.success
    assert "No session" in result.content


def test_unknown_action_fails(store):
    result = _tool(store).execute(action="fly")

    assert not result.success
    assert "Unknown action" in result.content


def test_missing_store_fails_loudly(tmp_path):
    result = CopilotSessionsTool(store_path=str(tmp_path / "nope.db")).execute()

    assert not result.success
    assert "not found" in result.content


def test_store_path_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(tmp_path / "custom.db"))

    assert CopilotSessionsTool().store_path == tmp_path / "custom.db"


def test_spec_is_openai_compatible(store):
    fn = _tool(store).to_openai_function()

    assert fn["function"]["name"] == "copilot_sessions"
    assert set(fn["function"]["parameters"]["properties"]["action"]["enum"]) == {
        "list",
        "idle",
        "detail",
    }
