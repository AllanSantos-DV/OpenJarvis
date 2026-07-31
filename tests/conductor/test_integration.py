"""Integration: the tick driven against a real session-store schema.

The unit suites use fakes. This one builds a SQLite database with the same
tables the Copilot app uses, drives the real ``CopilotSessionsTool`` through the
real reader adapter, and proves the loop end to end: an app session is answered,
a CLI session is never touched, and the reply must actually land.

The Copilot CLI itself is faked, because the assertion here is about the
orchestration; talking to the real binary is covered by the contract probes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.conductor.adapters import CopilotSessionsReader
from openjarvis.conductor.models import RetryPolicy
from openjarvis.conductor.policy import EligibilityPolicy, TierPolicy
from openjarvis.conductor.runner import (
    StartupError,
    check_credentials,
    check_unattended,
    main,
    run,
)
from openjarvis.conductor.runtime_lock import FileRuntimeLock
from openjarvis.conductor.service import ConductorService, ResumeResult
from openjarvis.conductor.state import SqliteClaimStore
from openjarvis.tools.copilot_sessions import CopilotSessionsTool

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


def _ago(minutes: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def session_store(tmp_path):
    """A store shaped like the real one: one app session and one CLI session."""
    path = tmp_path / "session-store.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO sessions (id, cwd, repository, host_type, branch, summary)"
        " VALUES ('app-1', 'C:/repo/projeto', 'owner/projeto', 'github', 'main',"
        " 'Implementando a fase 3')"
    )
    conn.execute(
        "INSERT INTO turns (session_id, turn_index, user_message, assistant_response,"
        " timestamp) VALUES ('app-1', 0, 'segue o plano', 'fiz a fase 2', ?)",
        (_ago(120),),
    )
    conn.execute(
        "INSERT INTO checkpoints (session_id, checkpoint_number, title, work_done,"
        " next_steps, created_at) VALUES ('app-1', 1, 'Fase 2', 'fase 2 pronta',"
        " 'implementar a fase 3', ?)",
        (_ago(120),),
    )

    conn.execute(
        "INSERT INTO sessions (id, cwd, host_type, summary)"
        " VALUES ('cli-1', 'C:/tmp', NULL, 'sessao do proprio CLI')"
    )
    conn.execute(
        "INSERT INTO turns (session_id, turn_index, user_message, assistant_response,"
        " timestamp) VALUES ('cli-1', 0, 'oi', 'ola', ?)",
        (_ago(200),),
    )

    conn.commit()
    conn.close()
    return path


class FakeCli:
    """Stands in for ``copilot --resume``: appends a turn to the target session."""

    def __init__(self, store_path, *, ok: bool = True, land: bool = True) -> None:
        self.store_path = store_path
        self.ok = ok
        self.land = land
        self.calls = []

    def resume(
        self, session_id: str, prompt: str, *, cwd: str = "", turns: int = 0
    ) -> ResumeResult:
        self.calls.append((session_id, prompt, cwd))
        if not self.ok:
            return ResumeResult(ok=False, error="cli failed")
        if self.land:
            conn = sqlite3.connect(self.store_path)
            nxt = conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) + 1 FROM turns"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message,"
                " assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (session_id, nxt, prompt, "retomei o trabalho", _ago(0)),
            )
            conn.commit()
            conn.close()
        return ResumeResult(ok=True, content="retomei o trabalho")


def _service(session_store, tmp_path, executor, **kw):
    reader = CopilotSessionsReader(CopilotSessionsTool(store_path=str(session_store)))
    claims = SqliteClaimStore(
        tmp_path / "claims.db", policy=RetryPolicy(lease_seconds=600)
    )
    kw.setdefault("eligibility", EligibilityPolicy(idle_minutes=10.0))
    kw.setdefault("tiers", TierPolicy(trivial_max_turns=5))
    kw.setdefault("readback_seconds", 0.0)
    service = ConductorService(reader=reader, claims=claims, executor=executor, **kw)
    return service, claims


def test_answers_the_app_session_and_records_the_turn(session_store, tmp_path):
    cli = FakeCli(session_store)
    service, claims = _service(session_store, tmp_path, cli)
    try:
        report = service.tick()
    finally:
        claims.close()

    assert [o.session_id for o in report.answered] == ["app-1"]
    assert cli.calls[0][0] == "app-1"
    assert "implementar a fase 3" in cli.calls[0][1]
    assert cli.calls[0][2] == "C:/repo/projeto"

    conn = sqlite3.connect(session_store)
    count = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = 'app-1'"
    ).fetchone()[0]
    conn.close()
    assert count == 2


def test_the_cli_session_is_never_touched(session_store, tmp_path):
    cli = FakeCli(session_store)
    service, claims = _service(session_store, tmp_path, cli)
    try:
        service.tick()
    finally:
        claims.close()

    assert [c[0] for c in cli.calls] == ["app-1"]

    conn = sqlite3.connect(session_store)
    count = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = 'cli-1'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_soak_repeated_ticks_do_not_duplicate_the_landed_reply(session_store, tmp_path):
    cli = FakeCli(session_store)
    service, claims = _service(session_store, tmp_path, cli)
    try:
        for _ in range(5):
            service.tick()
    finally:
        claims.close()

    # The reply has a fresh timestamp, so later ticks do not re-answer it.
    assert len(cli.calls) == 1


def test_a_reply_that_never_lands_is_not_reported_as_answered(session_store, tmp_path):
    cli = FakeCli(session_store, land=False)
    service, claims = _service(session_store, tmp_path, cli)
    try:
        report = service.tick()
    finally:
        claims.close()

    assert report.answered == []
    assert report.failed[0].detail == "no new turn recorded"


def test_conductor_writes_nothing_into_the_app_store(session_store, tmp_path):
    """Only the fake CLI may append turns; the conductor itself must not write."""
    before = session_store.stat().st_mtime_ns

    class NoopExecutor:
        def resume(self, session_id, prompt, *, cwd="", turns=0):
            return ResumeResult(ok=False, error="not sending")

    service, claims = _service(session_store, tmp_path, NoopExecutor())
    try:
        service.tick()
    finally:
        claims.close()

    conn = sqlite3.connect(session_store)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not row[0].startswith("sqlite_")  # SQLite's own AUTOINCREMENT bookkeeping
    }
    conn.close()

    assert tables == {"sessions", "turns", "checkpoints", "session_files"}
    assert session_store.stat().st_mtime_ns == before


def test_runner_refuses_to_start_without_a_token():
    with pytest.raises(StartupError, match="GH_TOKEN"):
        check_credentials({})


def test_runner_accepts_either_token_variable():
    check_credentials({"GH_TOKEN": "x"})
    check_credentials({"GITHUB_TOKEN": "y"})


def test_a_single_tick_per_launch_is_allowed():
    # The default, and the mode the desktop shortcut uses: the owner clicked,
    # the blast radius is one tick.
    check_unattended(interval=0, max_ticks=None, dry_run=False)


def test_watching_continuously_is_allowed():
    check_unattended(interval=300, max_ticks=None, dry_run=True)


def test_a_loop_of_declared_length_is_allowed():
    check_unattended(interval=300, max_ticks=5, dry_run=False)


def test_an_endless_answering_loop_is_refused():
    # This is the mode whose only real safeguard would be OS-level containment
    # -- an unprivileged user, a container, a scoped credential. None of that
    # exists yet, so the door is closed rather than left ajar behind a flag
    # that reads as innocently as `--interval 300`.
    with pytest.raises(StartupError, match="containment"):
        check_unattended(interval=300, max_ticks=None, dry_run=False)


def test_main_refuses_the_endless_loop_before_touching_anything(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    exit_code = main(["--interval", "300"])
    assert exit_code == 2


def test_runner_refuses_a_second_conductor(session_store, tmp_path):
    cli = FakeCli(session_store)
    service, claims = _service(session_store, tmp_path, cli)
    lock_path = tmp_path / "conductor.lock"
    holder = FileRuntimeLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(StartupError, match="Another Jarvis conductor"):
            run(service, lock=FileRuntimeLock(lock_path))
    finally:
        holder.release()
        claims.close()


def test_runner_releases_the_lock_after_a_single_tick(session_store, tmp_path):
    cli = FakeCli(session_store)
    service, claims = _service(session_store, tmp_path, cli)
    lock_path = tmp_path / "conductor.lock"
    try:
        report = run(service, lock=FileRuntimeLock(lock_path))
    finally:
        claims.close()

    assert report.acted == 1
    assert FileRuntimeLock(lock_path).acquire() is True
