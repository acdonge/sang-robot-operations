"""XR intervention latches, scoped roles, and two-person Run resume approval."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_runtime_models import OrchestrationRunTransitionV1
from fleet_control_plane.orchestration_store import OrchestrationStore
from fleet_control_plane.status import isoformat_z, parse_datetime


CONTROL_AUTHORITY_MIGRATION = """
CREATE TABLE IF NOT EXISTS control_role_bindings (
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('edge_operator', 'manager', 'safety_officer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, role)
);

CREATE TABLE IF NOT EXISTS xr_intervention_sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('edge_operator', 'manager')),
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    connected INTEGER NOT NULL CHECK (connected IN (0, 1)),
    focused INTEGER NOT NULL CHECK (focused IN (0, 1)),
    tracking INTEGER NOT NULL CHECK (tracking IN (0, 1)),
    deadman INTEGER NOT NULL CHECK (deadman IN (0, 1)),
    latched INTEGER NOT NULL CHECK (latched IN (0, 1)),
    latch_reason TEXT,
    sampled_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_challenges (
    challenge_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    held_revision INTEGER NOT NULL CHECK (held_revision >= 1),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'EXPIRED', 'INVALID')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_resume_per_run
    ON resume_challenges (run_id) WHERE state = 'PENDING';

CREATE TABLE IF NOT EXISTS resume_approvals (
    challenge_id TEXT NOT NULL REFERENCES resume_challenges(challenge_id) ON DELETE RESTRICT,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('edge_operator', 'manager')),
    session_id TEXT NOT NULL REFERENCES xr_intervention_sessions(session_id) ON DELETE RESTRICT,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (challenge_id, role),
    UNIQUE (challenge_id, principal_id)
);
"""


ControlRole = Literal["edge_operator", "manager", "safety_officer"]
XrRole = Literal["edge_operator", "manager"]
MAX_XR_SAMPLE_AGE = timedelta(seconds=1)


class XrSessionUpdateV1(FleetModel):
    schema_version: Literal[1] = 1
    session_id: UUID
    principal_id: SafeId
    role: XrRole
    run_id: SafeId
    sequence: int = Field(ge=0)
    connected: bool
    focused: bool
    tracking: bool
    deadman: bool
    sampled_at: AwareDatetime


class ControlRoleBindingV1(FleetModel):
    schema_version: Literal[1] = 1
    principal_id: SafeId
    role: ControlRole


class ResumeChallengeCreateV1(FleetModel):
    schema_version: Literal[1] = 1
    ttl_seconds: int = Field(default=120, ge=10, le=900)


class ResumeApprovalV1(FleetModel):
    schema_version: Literal[1] = 1
    session_id: UUID
    principal_id: SafeId | None = None


class ControlAuthorityError(ValueError):
    pass


class ControlAuthority:
    def __init__(self, database_path: str | Path, orchestration: OrchestrationStore) -> None:
        self.path = Path(database_path)
        self.orchestration = orchestration
        with self._connect() as connection:
            connection.executescript(CONTROL_AUTHORITY_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def bind_role(self, principal_id: str, role: ControlRole, *, now: datetime) -> dict:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO control_role_bindings (principal_id, role, created_at)
                VALUES (?, ?, ?)
                """,
                (principal_id, role, isoformat_z(now)),
            )
        return {"principal_id": principal_id, "role": role}

    def has_role(self, principal_id: str, role: ControlRole) -> bool:
        with self._connect() as connection:
            return self._has_role(connection, principal_id, role)

    def _has_role(self, connection: sqlite3.Connection, principal_id: str, role: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM control_role_bindings WHERE principal_id = ? AND role = ?",
            (principal_id, role),
        ).fetchone() is not None

    def update_xr_session(self, body: XrSessionUpdateV1, *, received_at: datetime) -> dict:
        received = parse_datetime(received_at)
        sampled = parse_datetime(body.sampled_at)
        if sampled > received:
            raise ControlAuthorityError("XR sample is future-dated")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._has_role(connection, body.principal_id, body.role):
                raise ControlAuthorityError("principal lacks the declared XR role")
            existing = connection.execute(
                "SELECT * FROM xr_intervention_sessions WHERE session_id = ?",
                (str(body.session_id),),
            ).fetchone()
            if existing is not None:
                identity = (existing["principal_id"], existing["role"], existing["run_id"])
                if identity != (body.principal_id, body.role, body.run_id):
                    raise ControlAuthorityError("XR session identity is immutable")
                if body.sequence <= existing["sequence"]:
                    raise ControlAuthorityError("XR sequence regression or duplicate")
                if sampled <= parse_datetime(existing["sampled_at"]):
                    raise ControlAuthorityError("XR sample time regression or duplicate")
            failed = [
                name
                for name, value in (
                    ("network", body.connected),
                    ("focus", body.focused),
                    ("tracking", body.tracking),
                    ("deadman", body.deadman),
                )
                if not value
            ]
            if received - sampled > MAX_XR_SAMPLE_AGE:
                failed.append("sample_age")
            latched = bool(failed) or bool(existing and existing["latched"])
            reason = (
                existing["latch_reason"]
                if existing is not None and existing["latched"]
                else ("lost:" + ",".join(failed) if failed else None)
            )
            created_at = (
                isoformat_z(received) if existing is None else existing["created_at"]
            )
            connection.execute(
                """
                INSERT INTO xr_intervention_sessions (
                    session_id, principal_id, role, run_id, sequence, connected,
                    focused, tracking, deadman, latched, latch_reason, sampled_at,
                    received_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    connected = excluded.connected,
                    focused = excluded.focused,
                    tracking = excluded.tracking,
                    deadman = excluded.deadman,
                    latched = excluded.latched,
                    latch_reason = excluded.latch_reason,
                    sampled_at = excluded.sampled_at,
                    received_at = excluded.received_at
                """,
                (
                    str(body.session_id),
                    body.principal_id,
                    body.role,
                    body.run_id,
                    body.sequence,
                    int(body.connected),
                    int(body.focused),
                    int(body.tracking),
                    int(body.deadman),
                    int(latched),
                    reason,
                    isoformat_z(sampled),
                    isoformat_z(received),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM xr_intervention_sessions WHERE session_id = ?",
                (str(body.session_id),),
            ).fetchone()
            return self._session(row)

    def require_active_xr_session(
        self,
        session_id: UUID | str,
        *,
        principal_id: str,
        run_id: str,
        now: datetime,
        max_age: timedelta = MAX_XR_SAMPLE_AGE,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM xr_intervention_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise ControlAuthorityError("unknown XR intervention session")
            if row["principal_id"] != principal_id or row["run_id"] != run_id:
                raise ControlAuthorityError("XR intervention session scope mismatch")
            current = parse_datetime(now)
            receive_age = current - parse_datetime(row["received_at"])
            sample_age = current - parse_datetime(row["sampled_at"])
            if (
                receive_age < timedelta(0)
                or receive_age > max_age
                or sample_age < timedelta(0)
                or sample_age > max_age
            ):
                connection.execute(
                    """
                    UPDATE xr_intervention_sessions
                    SET latched = 1, latch_reason = 'lost:heartbeat'
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                )
                raise ControlAuthorityError("XR heartbeat is stale and intervention is latched")
            if row["latched"] or not all(
                row[field] for field in ("connected", "focused", "tracking", "deadman")
            ):
                raise ControlAuthorityError("XR intervention session is latched")
            return self._session(row)

    def begin_resume(self, run_id: str, *, now: datetime, ttl: timedelta) -> dict:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
            raise ControlAuthorityError("resume challenge TTL must be within 15 minutes")
        run = self.orchestration.get_run(run_id)
        if run is None or run["state"] != "HELD":
            raise ControlAuthorityError("resume approval requires a HELD Run")
        challenge_id = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM resume_challenges WHERE run_id = ? AND state = 'PENDING'",
                (run_id,),
            ).fetchone()
            if existing is not None:
                return self._challenge(connection, existing)
            connection.execute(
                """
                INSERT INTO resume_challenges (
                    challenge_id, run_id, held_revision, state, created_at, expires_at
                ) VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    challenge_id,
                    run_id,
                    run["revision"],
                    isoformat_z(now),
                    isoformat_z(parse_datetime(now) + ttl),
                ),
            )
            row = connection.execute(
                "SELECT * FROM resume_challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
            return self._challenge(connection, row)

    def approve_resume(
        self,
        challenge_id: str,
        *,
        principal_id: str,
        session_id: UUID | str,
        now: datetime,
    ) -> dict:
        current = parse_datetime(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            challenge = connection.execute(
                "SELECT * FROM resume_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if challenge is None or challenge["state"] != "PENDING":
                raise ControlAuthorityError("resume challenge is not pending")
            if current >= parse_datetime(challenge["expires_at"]):
                connection.execute(
                    "UPDATE resume_challenges SET state = 'EXPIRED' WHERE challenge_id = ?",
                    (challenge_id,),
                )
                raise ControlAuthorityError("resume challenge has expired")
            roles = [
                role
                for role in ("edge_operator", "manager")
                if self._has_role(connection, principal_id, role)
            ]
            if len(roles) != 1:
                raise ControlAuthorityError("resume approver requires exactly one scoped role")
            session = connection.execute(
                "SELECT * FROM xr_intervention_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if session is None or (
                session["principal_id"] != principal_id
                or session["role"] != roles[0]
                or session["run_id"] != challenge["run_id"]
            ):
                raise ControlAuthorityError("XR session does not match the resume approver")
            age = current - parse_datetime(session["received_at"])
            if session["latched"] or age < timedelta(0) or age > timedelta(seconds=1):
                raise ControlAuthorityError("resume approval requires a live, unlatched XR session")
            try:
                connection.execute(
                    """
                    INSERT INTO resume_approvals (
                        challenge_id, principal_id, role, session_id, approved_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (challenge_id, principal_id, roles[0], str(session_id), isoformat_z(current)),
                )
            except sqlite3.IntegrityError as exc:
                raise ControlAuthorityError("duplicate or non-independent resume approval") from exc
            approvals = connection.execute(
                "SELECT role FROM resume_approvals WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchall()
            approved_roles = {row["role"] for row in approvals}
            if approved_roles != {"edge_operator", "manager"}:
                return self._challenge(connection, challenge)
            run = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?",
                (challenge["run_id"],),
            ).fetchone()
            if (
                run is None
                or run["state"] != "HELD"
                or run["revision"] != challenge["held_revision"]
            ):
                connection.execute(
                    "UPDATE resume_challenges SET state = 'INVALID' WHERE challenge_id = ?",
                    (challenge_id,),
                )
                invalid_run = True
                resumed = None
            else:
                invalid_run = False
                resumed = self.orchestration.transition_run_in_transaction(
                    connection,
                    run["run_id"],
                    OrchestrationRunTransitionV1(
                        expected_revision=run["revision"],
                        state="ACTIVE",
                    ),
                    occurred_at=current,
                    resume_authorized=True,
                )
            if not invalid_run:
                connection.execute(
                    """
                    UPDATE resume_challenges
                    SET state = 'APPROVED', completed_at = ? WHERE challenge_id = ?
                    """,
                    (isoformat_z(current), challenge_id),
                )
            row = connection.execute(
                "SELECT * FROM resume_challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
            result = self._challenge(connection, row)
        if invalid_run:
            raise ControlAuthorityError("HELD Run changed while approvals were collected")
        return {**result, "run_state": resumed["state"]}

    @staticmethod
    def _session(row: sqlite3.Row) -> dict:
        return {
            "session_id": row["session_id"],
            "principal_id": row["principal_id"],
            "role": row["role"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "connected": bool(row["connected"]),
            "focused": bool(row["focused"]),
            "tracking": bool(row["tracking"]),
            "deadman": bool(row["deadman"]),
            "latched": bool(row["latched"]),
            "latch_reason": row["latch_reason"],
            "sampled_at": row["sampled_at"],
            "received_at": row["received_at"],
        }

    @staticmethod
    def _challenge(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        approvals = [
            dict(item)
            for item in connection.execute(
                """
                SELECT principal_id, role, session_id, approved_at
                FROM resume_approvals WHERE challenge_id = ? ORDER BY role
                """,
                (row["challenge_id"],),
            ).fetchall()
        ]
        return {
            "challenge_id": row["challenge_id"],
            "run_id": row["run_id"],
            "held_revision": row["held_revision"],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "approvals": approvals,
            "physical_estop_claimed": False,
        }
