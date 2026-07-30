"""copilot_sessions -- read the state of every GitHub Copilot app session.

This is the piece that lets an assistant act as a *conductor* over the other
agent sessions running on the machine: instead of the user hopping between
sessions to read what happened, the assistant polls this tool, sees which
sessions went idle, reads the distilled outcome, and reports or escalates.

Data comes straight from the Copilot app's SQLite session store
(``~/.copilot/session-store.db``), opened **read-only**. That is deliberate:

* the store is written by the app and survives daemon restarts, so it stays
  authoritative even when the copilot-mobile bridge is down;
* ``checkpoints`` already holds the app's own distilled summary of a session
  (``title`` / ``overview`` / ``work_done`` / ``next_steps``), so the outcome of
  a finished session is available without re-reading its whole transcript.

Actions
-------
``list``
    Sessions ordered by most recent activity, with turn counts and idle time.
``idle``
    Only sessions quiet for at least ``idle_minutes`` -- the "it finished, go
    look at it" signal.
``detail``
    One session: metadata, latest checkpoint, files touched, last turns.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_STORE_ENV = "COPILOT_SESSION_STORE"
_DEFAULT_STORE = Path.home() / ".copilot" / "session-store.db"

_DEFAULT_LIMIT = 20
_DEFAULT_IDLE_MINUTES = 10


def default_store_path() -> Path:
    """Path of the Copilot app session store (``COPILOT_SESSION_STORE`` wins)."""
    override = os.environ.get(_STORE_ENV)
    return Path(override) if override else _DEFAULT_STORE


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse a store timestamp, tolerating both formats it writes.

    Rows mix SQLite's ``'YYYY-MM-DD HH:MM:SS'`` with ISO ``...Z`` strings, so a
    single format string is not enough.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _idle_minutes(last_activity: str) -> Optional[float]:
    """Minutes since *last_activity*, or ``None`` when it cannot be parsed."""
    parsed = _parse_ts(last_activity)
    if parsed is None:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return round(delta.total_seconds() / 60.0, 1)


@ToolRegistry.register("copilot_sessions")
class CopilotSessionsTool(BaseTool):
    """Inspect the GitHub Copilot app's sessions from its own store."""

    tool_id = "copilot_sessions"

    def __init__(self, store_path: Optional[str] = None) -> None:
        self._store_path = Path(store_path) if store_path else None

    @property
    def store_path(self) -> Path:
        return self._store_path or default_store_path()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="copilot_sessions",
            description=(
                "Inspect GitHub Copilot app sessions running on this machine. "
                "Use action='idle' to find sessions that stopped working and may "
                "need attention, action='list' for an overview, and "
                "action='detail' with a session_id to read what a session "
                "actually did (summary, next steps, files touched)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "idle", "detail"],
                        "description": "What to fetch (default: list).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session id, required for action='detail'.",
                    },
                    "idle_minutes": {
                        "type": "integer",
                        "description": (
                            "For action='idle': minimum minutes without activity "
                            f"(default: {_DEFAULT_IDLE_MINUTES})."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max sessions to return (default: {_DEFAULT_LIMIT})."
                        ),
                    },
                },
                "required": [],
            },
            category="sessions",
        )

    def _connect(self) -> sqlite3.Connection:
        """Open the store read-only so the running app is never disturbed."""
        path = self.store_path
        if not path.exists():
            raise FileNotFoundError(
                f"Copilot session store not found at {path}. Set "
                f"{_STORE_ENV} if the app stores it elsewhere."
            )
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _list_sessions(
        self,
        conn: sqlite3.Connection,
        limit: int,
        *,
        min_idle_minutes: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Sessions by recency, optionally keeping only the idle ones.

        Idle filtering happens in Python because the store mixes SQLite's
        ``'YYYY-MM-DD HH:MM:SS'`` timestamps with ISO ``...Z`` strings, which do
        not compare correctly in SQL. To keep that safe, the scan walks pages of
        candidates until it has filled *limit* rather than filtering a single
        truncated page -- otherwise a long-idle session would be invisible
        simply because recent noisy sessions filled the first page.
        """
        collected: List[Dict[str, Any]] = []
        offset = 0
        page = max(limit, 50) if min_idle_minutes is not None else limit

        while True:
            rows = conn.execute(
                """
                SELECT s.id, s.summary, s.cwd, s.repository, s.branch,
                       COUNT(t.id) AS turns, MAX(t.timestamp) AS last_activity
                FROM sessions s
                JOIN turns t ON t.session_id = s.id
                GROUP BY s.id
                ORDER BY last_activity DESC
                LIMIT ? OFFSET ?
                """,
                (page, offset),
            ).fetchall()

            if not rows:
                break

            for row in rows:
                idle = _idle_minutes(row["last_activity"] or "")
                if min_idle_minutes is not None and (
                    idle is None or idle < min_idle_minutes
                ):
                    continue
                collected.append(
                    {
                        "session_id": row["id"],
                        "summary": row["summary"] or "",
                        "cwd": row["cwd"] or "",
                        "repository": row["repository"] or "",
                        "branch": row["branch"] or "",
                        "turns": row["turns"],
                        "last_activity": row["last_activity"] or "",
                        "idle_minutes": idle,
                    }
                )
                if len(collected) >= limit:
                    return collected

            if min_idle_minutes is None or len(rows) < page:
                break
            offset += page

        return collected

    def _detail(self, conn: sqlite3.Connection, session_id: str) -> Dict[str, Any]:
        session = conn.execute(
            "SELECT id, summary, cwd, repository, branch, created_at, updated_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise LookupError(f"No session with id {session_id!r}")

        checkpoint = conn.execute(
            "SELECT title, overview, work_done, next_steps, created_at "
            "FROM checkpoints WHERE session_id = ? "
            "ORDER BY checkpoint_number DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        turns = conn.execute(
            "SELECT turn_index, user_message, assistant_response, timestamp "
            "FROM turns WHERE session_id = ? ORDER BY turn_index DESC LIMIT 3",
            (session_id,),
        ).fetchall()

        files = conn.execute(
            "SELECT file_path FROM session_files WHERE session_id = ? "
            "ORDER BY first_seen_at DESC LIMIT 20",
            (session_id,),
        ).fetchall()

        detail: Dict[str, Any] = {
            "session_id": session["id"],
            "summary": session["summary"] or "",
            "cwd": session["cwd"] or "",
            "repository": session["repository"] or "",
            "branch": session["branch"] or "",
            "created_at": session["created_at"] or "",
            "updated_at": session["updated_at"] or "",
            "files_touched": [row["file_path"] for row in files],
            "recent_turns": [
                {
                    "turn_index": row["turn_index"],
                    "user_message": row["user_message"] or "",
                    "assistant_response": row["assistant_response"] or "",
                    "timestamp": row["timestamp"] or "",
                }
                for row in reversed(turns)
            ],
        }

        if checkpoint is not None:
            detail["checkpoint"] = {
                "title": checkpoint["title"] or "",
                "overview": checkpoint["overview"] or "",
                "work_done": checkpoint["work_done"] or "",
                "next_steps": checkpoint["next_steps"] or "",
                "created_at": checkpoint["created_at"] or "",
            }

        if detail["recent_turns"]:
            detail["idle_minutes"] = _idle_minutes(
                detail["recent_turns"][-1]["timestamp"]
            )

        return detail

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "list").lower()
        limit = int(params.get("limit") or _DEFAULT_LIMIT)

        try:
            conn = self._connect()
        except FileNotFoundError as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)

        try:
            if action == "detail":
                session_id = str(params.get("session_id") or "").strip()
                if not session_id:
                    return ToolResult(
                        tool_name=self.tool_id,
                        content="action='detail' requires a session_id.",
                        success=False,
                    )
                payload: Dict[str, Any] = self._detail(conn, session_id)

            elif action in {"list", "idle"}:
                threshold = (
                    float(params.get("idle_minutes") or _DEFAULT_IDLE_MINUTES)
                    if action == "idle"
                    else None
                )
                sessions = self._list_sessions(
                    conn, max(limit, 1), min_idle_minutes=threshold
                )
                payload = {"sessions": sessions, "count": len(sessions)}

            else:
                return ToolResult(
                    tool_name=self.tool_id,
                    content=f"Unknown action {action!r}. Use list, idle or detail.",
                    success=False,
                )

        except LookupError as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)
        except sqlite3.Error as exc:
            logger.error("Failed to read Copilot session store: %s", exc)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Failed to read the session store: {exc}",
                success=False,
            )
        finally:
            conn.close()

        return ToolResult(tool_name=self.tool_id, content=payload, success=True)


__all__ = ["CopilotSessionsTool", "default_store_path"]
