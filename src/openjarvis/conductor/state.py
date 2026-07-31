"""SQLite-backed claim store: leases, retries and audit in the conductor's own DB.

Why a separate database at all: the Copilot app owns
``~/.copilot/session-store.db``. Writing bookkeeping into that schema would
couple us to another product's migrations and risk corrupting the user's real
session history, so every byte of conductor state lives here instead.

What this buys us, concretely:

* **One responder per turn.** ``acquire`` runs inside ``BEGIN IMMEDIATE``, so two
  ticks racing on the same observation cannot both win a lease.
* **A crash does not burn a turn.** Leases expire; ``recover_expired`` hands the
  work back with a retry, instead of leaving it stuck as "in progress" forever.
* **A late worker cannot finish someone else's work.** Every transition is
  conditional on the ``claim_token`` issued with the lease.

The store deliberately does *not* claim cross-database atomicity with the Copilot
app: it cannot. It provides optimistic control -- the caller still re-checks the
fingerprint before acting and records a conflict if the session moved.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from openjarvis.conductor.models import (
    ACTIVE_STATES,
    CONFLICT,
    FAILED,
    LEASED,
    OBSERVED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    AuditEvent,
    Claim,
    ObservationFingerprint,
    RetryPolicy,
    StaleClaimError,
)

logger = logging.getLogger(__name__)

_DB_ENV = "JARVIS_CONDUCTOR_DB"
_DEFAULT_DB = Path.home() / ".jarvis-conductor" / "claims.db"

#: Guard against ever pointing this store at the Copilot app's own database.
_FORBIDDEN_NAMES = frozenset({"session-store.db", "data.db"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    key               TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    turn_index        INTEGER NOT NULL,
    turn_timestamp    TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    state             TEXT NOT NULL,
    owner             TEXT NOT NULL DEFAULT '',
    claim_token       TEXT NOT NULL DEFAULT '',
    lease_expires_at  REAL NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    retry_at          REAL NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_claims_state ON claims(state);
CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id);

CREATE TABLE IF NOT EXISTS claim_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    fingerprint_key TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    event           TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '',
    claim_token     TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_key ON claim_audit(fingerprint_key);
"""


def default_db_path() -> Path:
    """Conductor DB location (``JARVIS_CONDUCTOR_DB`` overrides)."""
    override = os.environ.get(_DB_ENV)
    return Path(override) if override else _DEFAULT_DB


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        fingerprint=ObservationFingerprint(
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            timestamp=row["turn_timestamp"],
            content_hash=row["content_hash"],
        ),
        state=row["state"],
        owner=row["owner"],
        claim_token=row["claim_token"],
        lease_expires_at=row["lease_expires_at"],
        attempts=row["attempts"],
        retry_at=row["retry_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
    )


class SqliteClaimStore:
    """Durable claim state for the conductor. See module docstring."""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        *,
        policy: Optional[RetryPolicy] = None,
        clock=None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        path = Path(db_path) if db_path is not None else default_db_path()
        if path.name in _FORBIDDEN_NAMES:
            raise ValueError(
                f"Refusing to use {path} as the conductor store: that file belongs "
                "to the Copilot app. Conductor state must live in its own database."
            )
        self._path = path
        self._policy = policy or RetryPolicy()
        self._clock = clock or time.time
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), timeout=busy_timeout_ms / 1000.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._set_wal(self._conn)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _set_wal(conn: sqlite3.Connection) -> None:
        """Enable WAL, tolerating a concurrent opener.

        ``PRAGMA journal_mode = WAL`` needs a brief exclusive lock and returns
        ``SQLITE_BUSY`` *without* consulting the busy handler, so several
        conductors starting at the same instant would crash on open. The setting
        is persisted in the database file, so whichever connection wins applies
        it for everyone -- failing here is safe as long as it is not silent.
        """
        for _ in range(3):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                last = exc
                time.sleep(0.05)
        logger.debug("Could not set WAL (another opener won the race): %s", last)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """Write transaction that takes the reserved lock up front.

        ``BEGIN IMMEDIATE`` is what makes two racing ticks serialise: the loser
        blocks (up to ``busy_timeout``) instead of reading stale state and then
        overwriting the winner's lease.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _log(
        self,
        conn: sqlite3.Connection,
        fingerprint_key: str,
        session_id: str,
        event: str,
        *,
        owner: str = "",
        claim_token: str = "",
        detail: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO claim_audit (timestamp, fingerprint_key, session_id, event,"
            " owner, claim_token, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock(),
                fingerprint_key,
                session_id,
                event,
                owner,
                claim_token,
                detail,
            ),
        )
        logger.debug(
            "conductor.%s key=%s session=%s owner=%s token=%s %s",
            event,
            fingerprint_key,
            session_id,
            owner,
            claim_token,
            detail,
        )

    def _fetch(self, conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM claims WHERE key = ?", (key,)).fetchone()

    def observe(self, fingerprint: ObservationFingerprint) -> Claim:
        """Record the observation. Idempotent: re-seeing a turn never resets it."""
        with self._immediate() as conn:
            row = self._fetch(conn, fingerprint.key)
            if row is not None:
                return _row_to_claim(row)
            now = self._clock()
            conn.execute(
                "INSERT INTO claims (key, session_id, turn_index, turn_timestamp,"
                " content_hash, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    fingerprint.key,
                    fingerprint.session_id,
                    fingerprint.turn_index,
                    fingerprint.timestamp,
                    fingerprint.content_hash,
                    OBSERVED,
                    now,
                ),
            )
            self._log(conn, fingerprint.key, fingerprint.session_id, "observed")
            row = self._fetch(conn, fingerprint.key)
        return _row_to_claim(row)

    def acquire(
        self,
        fingerprint: ObservationFingerprint,
        owner: str,
        *,
        lease_seconds: Optional[float] = None,
    ) -> Optional[Claim]:
        """Take a lease on *fingerprint*, or return ``None`` when not claimable.

        Not claimable means: already finished, already conflicted, out of
        attempts, still backing off, or actively leased by someone else.
        """
        lease = float(
            lease_seconds if lease_seconds is not None else self._policy.lease_seconds
        )
        token = uuid.uuid4().hex
        with self._immediate() as conn:
            row = self._fetch(conn, fingerprint.key)
            now = self._clock()

            if row is None:
                conn.execute(
                    "INSERT INTO claims (key, session_id, turn_index, turn_timestamp,"
                    " content_hash, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        fingerprint.key,
                        fingerprint.session_id,
                        fingerprint.turn_index,
                        fingerprint.timestamp,
                        fingerprint.content_hash,
                        OBSERVED,
                        now,
                    ),
                )
                self._log(conn, fingerprint.key, fingerprint.session_id, "observed")
                row = self._fetch(conn, fingerprint.key)

            state = row["state"]
            if state in TERMINAL_STATES:
                self._log(
                    conn,
                    fingerprint.key,
                    fingerprint.session_id,
                    "skipped",
                    owner=owner,
                    detail=f"terminal:{state}",
                )
                return None

            if state in ACTIVE_STATES and row["lease_expires_at"] > now:
                self._log(
                    conn,
                    fingerprint.key,
                    fingerprint.session_id,
                    "skipped",
                    owner=owner,
                    detail=f"lease held by {row['owner']}",
                )
                return None

            if row["attempts"] >= self._policy.max_attempts:
                self._log(
                    conn,
                    fingerprint.key,
                    fingerprint.session_id,
                    "skipped",
                    owner=owner,
                    detail=f"max attempts ({self._policy.max_attempts}) reached",
                )
                return None

            if row["retry_at"] > now:
                self._log(
                    conn,
                    fingerprint.key,
                    fingerprint.session_id,
                    "skipped",
                    owner=owner,
                    detail="backing off",
                )
                return None

            conn.execute(
                "UPDATE claims SET state = ?, owner = ?, claim_token = ?,"
                " lease_expires_at = ?, attempts = attempts + 1, updated_at = ?,"
                " last_error = '' WHERE key = ?",
                (LEASED, owner, token, now + lease, now, fingerprint.key),
            )
            self._log(
                conn,
                fingerprint.key,
                fingerprint.session_id,
                "claimed",
                owner=owner,
                claim_token=token,
            )
            row = self._fetch(conn, fingerprint.key)
        return _row_to_claim(row)

    def _transition(
        self,
        key: str,
        claim_token: str,
        *,
        from_states: frozenset,
        to_state: str,
        event: str,
        detail: str = "",
        error: str = "",
    ) -> Claim:
        """Advance a claim only if the caller still owns the lease."""
        with self._immediate() as conn:
            row = self._fetch(conn, key)
            if row is None:
                raise StaleClaimError(f"No claim for key {key!r}")
            if row["claim_token"] != claim_token or row["state"] not in from_states:
                self._log(
                    conn,
                    key,
                    row["session_id"],
                    "stale_transition",
                    claim_token=claim_token,
                    detail=f"state={row['state']} owner_token={row['claim_token']}",
                )
                raise StaleClaimError(
                    f"Claim {key!r} is no longer owned by token {claim_token!r} "
                    f"(state={row['state']!r})"
                )

            now = self._clock()
            retry_at = 0.0
            lease = row["lease_expires_at"]
            token = claim_token
            if to_state == RUNNING:
                lease = now + self._policy.lease_seconds
            elif to_state == FAILED:
                retry_at = now + self._policy.backoff_for(row["attempts"])
                lease = 0.0
                token = ""
            elif to_state in TERMINAL_STATES:
                lease = 0.0
                token = ""

            conn.execute(
                "UPDATE claims SET state = ?, lease_expires_at = ?, retry_at = ?,"
                " updated_at = ?, last_error = ?, claim_token = ? WHERE key = ?"
                " AND claim_token = ?",
                (to_state, lease, retry_at, now, error, token, key, claim_token),
            )
            self._log(
                conn,
                key,
                row["session_id"],
                event,
                owner=row["owner"],
                claim_token=claim_token,
                detail=detail or error,
            )
            row = self._fetch(conn, key)
        return _row_to_claim(row)

    def release(self, key: str, claim_token: str, *, reason: str = "") -> Claim:
        """Hand the claim back untouched: no attempt spent, no backoff.

        Waiting for a human decision is not a failed attempt. Marking it as one
        would burn a retry and park the work behind a backoff, so the tick that
        finally sees the approval could not act on it -- the approval would look
        ignored.
        """
        with self._immediate() as conn:
            row = self._fetch(conn, key)
            if row is None or row["claim_token"] != claim_token:
                raise StaleClaimError(f"Claim {key!r} is not held by {claim_token!r}")
            conn.execute(
                "UPDATE claims SET state = ?, claim_token = '', lease_expires_at = 0,"
                " retry_at = 0, attempts = MAX(attempts - 1, 0), updated_at = ?,"
                " last_error = ? WHERE key = ? AND claim_token = ?",
                (OBSERVED, self._clock(), reason, key, claim_token),
            )
            self._log(
                conn,
                key,
                row["session_id"],
                "released",
                owner=row["owner"],
                claim_token=claim_token,
                detail=reason,
            )
            row = self._fetch(conn, key)
        return _row_to_claim(row)

    def mark_running(self, key: str, claim_token: str) -> Claim:
        return self._transition(
            key,
            claim_token,
            from_states=frozenset({LEASED}),
            to_state=RUNNING,
            event="running",
        )

    def mark_succeeded(self, key: str, claim_token: str, *, detail: str = "") -> Claim:
        return self._transition(
            key,
            claim_token,
            from_states=ACTIVE_STATES,
            to_state=SUCCEEDED,
            event="succeeded",
            detail=detail,
        )

    def mark_failed(self, key: str, claim_token: str, *, error: str = "") -> Claim:
        return self._transition(
            key,
            claim_token,
            from_states=ACTIVE_STATES,
            to_state=FAILED,
            event="failed",
            error=error,
        )

    def mark_conflict(self, key: str, claim_token: str, *, detail: str = "") -> Claim:
        return self._transition(
            key,
            claim_token,
            from_states=ACTIVE_STATES,
            to_state=CONFLICT,
            event="conflict",
            detail=detail,
        )

    def recover_expired(self) -> List[Claim]:
        """Hand orphaned leases back for retry.

        Without this, a conductor killed mid-run would leave the turn pinned as
        ``running`` and nobody would ever answer that session again.
        """
        recovered: List[Claim] = []
        with self._immediate() as conn:
            now = self._clock()
            rows = conn.execute(
                "SELECT * FROM claims WHERE state IN (?, ?) AND lease_expires_at <= ?",
                (LEASED, RUNNING, now),
            ).fetchall()
            for row in rows:
                retry_at = now + self._policy.backoff_for(row["attempts"])
                conn.execute(
                    "UPDATE claims SET state = ?, claim_token = '',"
                    " lease_expires_at = 0, retry_at = ?, updated_at = ?,"
                    " last_error = ? WHERE key = ?",
                    (FAILED, retry_at, now, "lease expired", row["key"]),
                )
                self._log(
                    conn,
                    row["key"],
                    row["session_id"],
                    "recovered",
                    owner=row["owner"],
                    claim_token=row["claim_token"],
                    detail="orphaned lease reclaimed",
                )
                recovered.append(_row_to_claim(self._fetch(conn, row["key"])))
        return recovered

    def get(self, key: str) -> Optional[Claim]:
        row = self._fetch(self._conn, key)
        return _row_to_claim(row) if row is not None else None

    def audit(self, limit: int = 100) -> List[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM claim_audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            AuditEvent(
                timestamp=row["timestamp"],
                fingerprint_key=row["fingerprint_key"],
                session_id=row["session_id"],
                event=row["event"],
                owner=row["owner"],
                claim_token=row["claim_token"],
                detail=row["detail"],
            )
            for row in rows
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - close is best effort
            pass


__all__ = ["SqliteClaimStore", "default_db_path"]
