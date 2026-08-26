"""Durable Task Run-scoped capture-job orchestration."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, model_validator

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.status import isoformat_z, parse_datetime


ACTIVE_CAPTURE_STATES = (
    "PENDING",
    "CLAIMED",
    "RECORDING",
    "FINALIZING",
    "UPLOADING",
)
TERMINAL_CAPTURE_STATES = ("COMPLETE", "FAILED", "CANCELLED")
CAPTURE_JOB_MIGRATION = """
CREATE TABLE IF NOT EXISTS capture_workers (
    worker_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_registrations(agent_id)
        ON DELETE RESTRICT,
    robot_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (agent_id, profile_id)
);

CREATE INDEX IF NOT EXISTS capture_workers_agent
    ON capture_workers (agent_id, robot_id, last_seen_at);

CREATE TABLE IF NOT EXISTS capture_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES task_runs(run_id) ON DELETE RESTRICT,
    worker_id TEXT NOT NULL REFERENCES capture_workers(worker_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    duration_s INTEGER NOT NULL CHECK (duration_s BETWEEN 5 AND 28800),
    state TEXT NOT NULL CHECK (
        state IN (
            'PENDING', 'CLAIMED', 'RECORDING', 'FINALIZING',
            'UPLOADING', 'COMPLETE', 'FAILED', 'CANCELLED'
        )
    ),
    lease_id TEXT,
    stop_requested_at TEXT,
    error TEXT,
    created_by TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    last_worker_seen_at TEXT,
    UNIQUE (run_id, request_id),
    CHECK ((state = 'FAILED') OR error IS NULL),
    CHECK ((state IN ('CLAIMED', 'RECORDING', 'FINALIZING', 'UPLOADING')) = (lease_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS capture_jobs_active_worker
    ON capture_jobs (worker_id)
    WHERE state IN ('PENDING', 'CLAIMED', 'RECORDING', 'FINALIZING', 'UPLOADING');

CREATE INDEX IF NOT EXISTS capture_jobs_run
    ON capture_jobs (run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS capture_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES capture_jobs(job_id) ON DELETE RESTRICT,
    occurred_at TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('operator', 'agent', 'central')),
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS capture_job_events_job
    ON capture_job_events (job_id, id);
"""


class CaptureJobNotFound(ValueError):
    """The requested capture worker or job does not exist."""


class CaptureJobConflict(ValueError):
    """The request conflicts with durable capture state."""


class CaptureJobInvalid(ValueError):
    """The request violates Task Run or fixed-profile constraints."""


class CaptureJobCreate(FleetModel):
    request_id: SafeId
    worker_id: SafeId
    duration_s: int = Field(ge=5, le=28800)


class CaptureWorkerPoll(FleetModel):
    worker_id: SafeId
    profile_id: SafeId
    robot_id: SafeId
    asset_ids: list[SafeId] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def assets_are_unique(self) -> "CaptureWorkerPoll":
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        return self


CaptureReportState = Literal[
    "RECORDING",
    "FINALIZING",
    "UPLOADING",
    "FAILED",
]


class CaptureJobReport(FleetModel):
    worker_id: SafeId
    lease_id: SafeId
    state: CaptureReportState
    error: Annotated[
        str,
        StringConstraints(min_length=1, max_length=2048, strip_whitespace=True),
    ] | None = None

    @model_validator(mode="after")
    def failure_has_error(self) -> "CaptureJobReport":
        if (self.state == "FAILED") != (self.error is not None):
            raise ValueError("only FAILED reports require an error")
        return self


class CaptureJobStore:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        occurred_at: datetime,
        actor_kind: str,
        actor_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO capture_job_events (
                job_id, occurred_at, actor_kind, actor_id, event_type,
                from_state, to_state, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                isoformat_z(occurred_at),
                actor_kind,
                actor_id,
                event_type,
                from_state,
                to_state,
                json.dumps(
                    details or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    @staticmethod
    def _worker_from_row(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
        result = {
            "worker_id": row["worker_id"],
            "agent_id": row["agent_id"],
            "robot_id": row["robot_id"],
            "profile_id": row["profile_id"],
            "asset_ids": json.loads(row["asset_ids_json"]),
            "last_seen_at": row["last_seen_at"],
        }
        if now is not None:
            result["age_s"] = max(
                0.0,
                round((now - parse_datetime(row["last_seen_at"])).total_seconds(), 3),
            )
        return result

    @staticmethod
    def _recording_summary(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT state, total_size_bytes, expected_file_count,
                   completed_file_count, manifest_sha256, completed_at
            FROM recording_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    @classmethod
    def _job_from_row(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_events: bool = False,
        include_lease: bool = False,
    ) -> dict[str, Any]:
        task = connection.execute(
            """
            SELECT task_definition_id, task_version
            FROM task_runs WHERE run_id = ?
            """,
            (row["run_id"],),
        ).fetchone()
        worker = connection.execute(
            "SELECT * FROM capture_workers WHERE worker_id = ?",
            (row["worker_id"],),
        ).fetchone()
        result = {
            key: row[key]
            for key in (
                "job_id",
                "request_id",
                "run_id",
                "worker_id",
                "agent_id",
                "robot_id",
                "profile_id",
                "session_id",
                "duration_s",
                "state",
                "stop_requested_at",
                "error",
                "created_by",
                "revision",
                "created_at",
                "updated_at",
                "claimed_at",
                "started_at",
                "finished_at",
                "last_worker_seen_at",
            )
        }
        if include_lease:
            result["lease_id"] = row["lease_id"]
        result["task_definition"] = {
            "id": task["task_definition_id"],
            "version": task["task_version"],
        }
        result["asset_ids"] = json.loads(worker["asset_ids_json"])
        result["recording"] = cls._recording_summary(connection, row["session_id"])
        if include_events:
            events = connection.execute(
                "SELECT * FROM capture_job_events WHERE job_id = ? ORDER BY id",
                (row["job_id"],),
            ).fetchall()
            result["events"] = [
                {
                    **{key: event[key] for key in event.keys() if key != "details_json"},
                    "details": json.loads(event["details_json"]),
                }
                for event in events
            ]
        return result

    def register_worker_and_claim(
        self,
        body: CaptureWorkerPoll,
        *,
        agent_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        canonical_assets = sorted(body.asset_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in canonical_assets)
            rows = connection.execute(
                f"""
                SELECT asset_id, asset_type, lifecycle_state, agent_id, robot_id
                FROM assets WHERE asset_id IN ({placeholders})
                """,
                canonical_assets,
            ).fetchall()
            if {row["asset_id"] for row in rows} != set(canonical_assets):
                raise CaptureJobInvalid("capture worker references unknown assets")
            if not any(row["asset_type"] == "robot" for row in rows):
                raise CaptureJobInvalid("capture worker profile must include one robot asset")
            for row in rows:
                if row["lifecycle_state"] != "active":
                    raise CaptureJobInvalid(
                        f"capture worker asset is decommissioned: {row['asset_id']}"
                    )
                if row["asset_type"] not in {"robot", "camera"}:
                    raise CaptureJobInvalid(
                        f"capture worker asset is not a robot or camera: {row['asset_id']}"
                    )
                if row["agent_id"] != agent_id or row["robot_id"] != body.robot_id:
                    raise CaptureJobInvalid(
                        f"capture worker asset binding differs from its Agent/robot: {row['asset_id']}"
                    )

            encoded_assets = json.dumps(canonical_assets, separators=(",", ":"))
            existing = connection.execute(
                "SELECT * FROM capture_workers WHERE worker_id = ?",
                (body.worker_id,),
            ).fetchone()
            if existing is None:
                conflict = connection.execute(
                    """
                    SELECT worker_id FROM capture_workers
                    WHERE agent_id = ? AND profile_id = ?
                    """,
                    (agent_id, body.profile_id),
                ).fetchone()
                if conflict is not None:
                    raise CaptureJobConflict(
                        f"capture profile is already registered by worker {conflict['worker_id']}"
                    )
                connection.execute(
                    """
                    INSERT INTO capture_workers (
                        worker_id, agent_id, robot_id, profile_id,
                        asset_ids_json, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.worker_id,
                        agent_id,
                        body.robot_id,
                        body.profile_id,
                        encoded_assets,
                        timestamp,
                    ),
                )
            else:
                expected = (
                    agent_id,
                    body.robot_id,
                    body.profile_id,
                    encoded_assets,
                )
                actual = (
                    existing["agent_id"],
                    existing["robot_id"],
                    existing["profile_id"],
                    existing["asset_ids_json"],
                )
                if actual != expected:
                    raise CaptureJobConflict(
                        "capture worker identity or fixed profile changed"
                    )
                connection.execute(
                    "UPDATE capture_workers SET last_seen_at = ? WHERE worker_id = ?",
                    (timestamp, body.worker_id),
                )

            job = connection.execute(
                """
                SELECT * FROM capture_jobs
                WHERE worker_id = ? AND state IN (
                    'PENDING', 'CLAIMED', 'RECORDING', 'FINALIZING', 'UPLOADING'
                )
                ORDER BY created_at, job_id LIMIT 1
                """,
                (body.worker_id,),
            ).fetchone()
            if job is None:
                return {"worker": self._worker_from_row(
                    connection.execute(
                        "SELECT * FROM capture_workers WHERE worker_id = ?",
                        (body.worker_id,),
                    ).fetchone()
                ), "job": None}

            if job["state"] == "PENDING":
                lease_id = uuid4().hex
                connection.execute(
                    """
                    UPDATE capture_jobs
                    SET state = 'CLAIMED', lease_id = ?, revision = revision + 1,
                        claimed_at = ?, updated_at = ?, last_worker_seen_at = ?
                    WHERE job_id = ? AND state = 'PENDING'
                    """,
                    (lease_id, timestamp, timestamp, timestamp, job["job_id"]),
                )
                self._event(
                    connection,
                    job_id=job["job_id"],
                    occurred_at=occurred_at,
                    actor_kind="agent",
                    actor_id=agent_id,
                    event_type="claimed",
                    from_state="PENDING",
                    to_state="CLAIMED",
                    details={"worker_id": body.worker_id},
                )
            else:
                connection.execute(
                    """
                    UPDATE capture_jobs SET last_worker_seen_at = ?
                    WHERE job_id = ?
                    """,
                    (timestamp, job["job_id"]),
                )
            current = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            return {
                "worker": self._worker_from_row(
                    connection.execute(
                        "SELECT * FROM capture_workers WHERE worker_id = ?",
                        (body.worker_id,),
                    ).fetchone()
                ),
                "job": self._job_from_row(connection, current, include_lease=True),
            }

    def create_job(
        self,
        run_id: str,
        body: CaptureJobCreate,
        *,
        created_by: str,
        occurred_at: datetime,
        worker_ttl_s: float = 15.0,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM capture_jobs WHERE run_id = ? AND request_id = ?",
                (run_id, body.request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["worker_id"] != body.worker_id
                    or existing["duration_s"] != body.duration_s
                ):
                    raise CaptureJobConflict(
                        "capture request_id was already used with different parameters"
                    )
                return {
                    **self._job_from_row(connection, existing),
                    "idempotent_replay": True,
                }

            run = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise CaptureJobNotFound(f"unknown Task Run: {run_id}")
            if run["state"] != "ACTIVE":
                raise CaptureJobInvalid("capture jobs require an ACTIVE Task Run")
            worker = connection.execute(
                "SELECT * FROM capture_workers WHERE worker_id = ?",
                (body.worker_id,),
            ).fetchone()
            if worker is None:
                raise CaptureJobNotFound(f"unknown capture worker: {body.worker_id}")
            worker_age_s = (occurred_at - parse_datetime(worker["last_seen_at"])).total_seconds()
            if worker_age_s < -2.0 or worker_age_s > worker_ttl_s:
                raise CaptureJobInvalid("capture worker is offline or stale")
            assigned = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT asset_id FROM assignments WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
            profile_assets = set(json.loads(worker["asset_ids_json"]))
            missing = sorted(profile_assets - assigned)
            if missing:
                raise CaptureJobInvalid(
                    f"capture profile assets are not assigned to the Task Run: {missing}"
                )

            job_id = f"capture-{uuid4().hex}"
            timestamp = isoformat_z(occurred_at)
            try:
                connection.execute(
                    """
                    INSERT INTO capture_jobs (
                        job_id, request_id, run_id, worker_id, agent_id,
                        robot_id, profile_id, session_id, duration_s,
                        state, created_by, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 1, ?, ?)
                    """,
                    (
                        job_id,
                        body.request_id,
                        run_id,
                        worker["worker_id"],
                        worker["agent_id"],
                        worker["robot_id"],
                        worker["profile_id"],
                        job_id,
                        body.duration_s,
                        created_by,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "capture_jobs.worker_id" in str(exc):
                    raise CaptureJobConflict(
                        "capture worker already has a non-terminal job"
                    ) from exc
                raise
            self._event(
                connection,
                job_id=job_id,
                occurred_at=occurred_at,
                actor_kind="operator",
                actor_id=created_by,
                event_type="created",
                from_state=None,
                to_state="PENDING",
                details={"duration_s": body.duration_s},
            )
            row = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return {**self._job_from_row(connection, row), "idempotent_replay": False}

    def request_stop(
        self,
        job_id: str,
        *,
        requested_by: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CaptureJobNotFound(f"unknown capture job: {job_id}")
            if row["state"] in TERMINAL_CAPTURE_STATES or row["stop_requested_at"] is not None:
                return self._job_from_row(connection, row)
            if row["state"] == "PENDING":
                connection.execute(
                    """
                    UPDATE capture_jobs
                    SET state = 'CANCELLED', revision = revision + 1,
                        stop_requested_at = ?, finished_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (timestamp, timestamp, timestamp, job_id),
                )
                to_state = "CANCELLED"
            else:
                connection.execute(
                    """
                    UPDATE capture_jobs
                    SET stop_requested_at = ?, revision = revision + 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (timestamp, timestamp, job_id),
                )
                to_state = row["state"]
            self._event(
                connection,
                job_id=job_id,
                occurred_at=occurred_at,
                actor_kind="operator",
                actor_id=requested_by,
                event_type="stop_requested",
                from_state=row["state"],
                to_state=to_state,
            )
            current = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._job_from_row(connection, current)

    def report_job(
        self,
        job_id: str,
        body: CaptureJobReport,
        *,
        agent_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        allowed = {
            "CLAIMED": {"RECORDING", "FAILED"},
            "RECORDING": {"RECORDING", "FINALIZING", "UPLOADING", "FAILED"},
            "FINALIZING": {"FINALIZING", "UPLOADING", "FAILED"},
            "UPLOADING": {"UPLOADING", "FAILED"},
        }
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CaptureJobNotFound(f"unknown capture job: {job_id}")
            if row["agent_id"] != agent_id or row["worker_id"] != body.worker_id:
                raise CaptureJobInvalid("capture job does not belong to this Agent worker")
            if row["state"] == "COMPLETE" and body.state == "UPLOADING":
                return self._job_from_row(connection, row)
            if (
                row["state"] == "FAILED"
                and body.state == "FAILED"
                and row["error"] == body.error
            ):
                return self._job_from_row(connection, row)
            if row["lease_id"] != body.lease_id:
                raise CaptureJobConflict("capture job lease does not match")
            if body.state not in allowed.get(row["state"], set()):
                raise CaptureJobConflict(
                    f"invalid capture job transition: {row['state']} -> {body.state}"
                )
            if body.state == row["state"]:
                connection.execute(
                    "UPDATE capture_jobs SET last_worker_seen_at = ? WHERE job_id = ?",
                    (timestamp, job_id),
                )
            else:
                started_at = timestamp if body.state == "RECORDING" else row["started_at"]
                finished_at = timestamp if body.state == "FAILED" else row["finished_at"]
                connection.execute(
                    """
                    UPDATE capture_jobs
                    SET state = ?, error = ?, revision = revision + 1,
                        started_at = ?, finished_at = ?, updated_at = ?,
                        last_worker_seen_at = ?,
                        lease_id = CASE WHEN ? = 'FAILED' THEN NULL ELSE lease_id END
                    WHERE job_id = ?
                    """,
                    (
                        body.state,
                        body.error,
                        started_at,
                        finished_at,
                        timestamp,
                        timestamp,
                        body.state,
                        job_id,
                    ),
                )
                self._event(
                    connection,
                    job_id=job_id,
                    occurred_at=occurred_at,
                    actor_kind="agent",
                    actor_id=agent_id,
                    event_type="state_reported",
                    from_state=row["state"],
                    to_state=body.state,
                    details=None if body.error is None else {"error": body.error},
                )
            current = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._job_from_row(connection, current)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capture_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CaptureJobNotFound(f"unknown capture job: {job_id}")
            return self._job_from_row(connection, row, include_events=True)

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capture_workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                raise CaptureJobNotFound(f"unknown capture worker: {worker_id}")
            return self._worker_from_row(row)

    def list_jobs(self, *, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("capture job limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM capture_jobs WHERE run_id = ?
                ORDER BY created_at DESC, job_id LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
            return [self._job_from_row(connection, row) for row in rows]

    def list_workers_for_run(
        self,
        run_id: str,
        *,
        now: datetime,
        worker_ttl_s: float = 15.0,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT 1 FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise CaptureJobNotFound(f"unknown Task Run: {run_id}")
            assigned = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT asset_id FROM assignments WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
            workers = []
            for row in connection.execute(
                "SELECT * FROM capture_workers ORDER BY worker_id"
            ).fetchall():
                assets = set(json.loads(row["asset_ids_json"]))
                if not assets.issubset(assigned):
                    continue
                worker = self._worker_from_row(row, now=now)
                worker["available"] = worker["age_s"] <= worker_ttl_s
                workers.append(worker)
            return workers
