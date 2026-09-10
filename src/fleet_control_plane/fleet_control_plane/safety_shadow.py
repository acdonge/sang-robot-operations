"""Deterministic safety Shadow Mode evaluator, persistence, and replay CLI."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence

from fleet_control_plane.safety_models import (
    ClockQuality,
    SafetyAssessmentV1,
    SafetyPolicyV1,
    SafetyReasonV1,
    SafetySnapshotV1,
    safety_contract_schema,
)
from fleet_control_plane.status import isoformat_z, parse_datetime


SAFETY_SHADOW_MIGRATION = """
CREATE TABLE IF NOT EXISTS safety_latest (
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES task_runs(run_id) ON DELETE RESTRICT,
    evaluator_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    evaluator_version TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
    state TEXT NOT NULL CHECK (state IN ('SAFE', 'WARNING', 'UNSAFE', 'UNKNOWN')),
    reason_signature TEXT NOT NULL CHECK (length(reason_signature) = 64),
    assessed_at TEXT NOT NULL,
    input_captured_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    asset_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    received_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, run_id, evaluator_id)
);

CREATE INDEX IF NOT EXISTS safety_latest_run
    ON safety_latest (run_id, state, assessed_at DESC);

CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES task_runs(run_id) ON DELETE RESTRICT,
    evaluator_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('SAFE', 'WARNING', 'UNSAFE', 'UNKNOWN')),
    reason_signature TEXT NOT NULL CHECK (length(reason_signature) = 64),
    assessed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS safety_events_run_id
    ON safety_events (run_id, id DESC);
CREATE INDEX IF NOT EXISTS safety_events_agent_id
    ON safety_events (agent_id, id DESC);
"""
MAX_SAFETY_EVENTS = 10_000
MAX_ASSESSMENT_FUTURE_SKEW_MS = 2_000


class SafetyAssessmentConflict(ValueError):
    """An assessment is replayed with different content or arrives out of order."""


class SafetyAssessmentInvalid(ValueError):
    """An assessment violates its Task Run or asset scope."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _transition_signature(assessment: SafetyAssessmentV1) -> str:
    identity_detail_keys = {
        "entity_id",
        "expected_step",
        "observed_step",
        "signal_ids",
        "zone_id",
    }
    reasons = []
    for reason in assessment.reasons:
        reasons.append(
            {
                "code": reason.code,
                "severity": reason.severity,
                "asset_ids": sorted(reason.asset_ids),
                "identity_details": {
                    key: value
                    for key, value in reason.details.items()
                    if key in identity_detail_keys
                },
            }
        )
    reasons.sort(key=_canonical_json)
    return _digest(
        {
            "evaluator_version": assessment.evaluator_version,
            "policy_id": assessment.policy_id,
            "policy_version": assessment.policy_version,
            "reasons": reasons,
        }
    )


def _reason(
    code: str,
    severity: str,
    message: str,
    *,
    asset_ids: Sequence[str] = (),
    details: dict[str, Any] | None = None,
) -> SafetyReasonV1:
    return SafetyReasonV1(
        code=code,
        severity=severity,
        message=message,
        asset_ids=sorted(set(asset_ids)),
        details=details or {},
    )


def evaluate_snapshot(
    snapshot: SafetySnapshotV1,
    policy: SafetyPolicyV1,
    *,
    assessed_at: datetime,
    evaluator_id: str = "sang-reference-shadow",
    evaluator_version: str = "0.1.0",
    measured_clock_quality: ClockQuality | None = None,
) -> SafetyAssessmentV1:
    """Evaluate derived Unity facts without publishing or invoking any command."""

    current = parse_datetime(assessed_at)
    captured = parse_datetime(snapshot.captured_at)
    valid_until = captured + timedelta(milliseconds=snapshot.ttl_ms)
    reasons: list[SafetyReasonV1] = []

    age_ms = (current - captured).total_seconds() * 1_000.0
    if age_ms > snapshot.ttl_ms:
        reasons.append(
            _reason(
                "INPUT_STALE",
                "UNKNOWN",
                "The safety snapshot exceeded its validity window.",
                asset_ids=snapshot.asset_ids,
                details={"age_ms": round(age_ms, 3), "ttl_ms": snapshot.ttl_ms},
            )
        )
    elif age_ms < -policy.future_tolerance_ms:
        reasons.append(
            _reason(
                "INPUT_TIME_INVALID",
                "UNKNOWN",
                "The safety snapshot timestamp is too far in the future.",
                asset_ids=snapshot.asset_ids,
                details={"age_ms": round(age_ms, 3)},
            )
        )

    invalid_signals = sorted(
        signal.signal_id
        for signal in snapshot.signals
        if signal.required and not signal.valid
    )
    if invalid_signals:
        reasons.append(
            _reason(
                "REQUIRED_SIGNAL_INVALID",
                "UNKNOWN",
                "One or more required safety inputs are invalid.",
                details={"signal_ids": invalid_signals},
            )
        )

    missing_signal_confidence = sorted(
        signal.signal_id
        for signal in snapshot.signals
        if signal.required and signal.valid and signal.confidence is None
    )
    if missing_signal_confidence:
        reasons.append(
            _reason(
                "REQUIRED_SIGNAL_CONFIDENCE_MISSING",
                "UNKNOWN",
                "One or more required safety inputs lack measured confidence.",
                details={"signal_ids": missing_signal_confidence},
            )
        )

    stale_signals: list[str] = []
    future_signals: list[str] = []
    for signal in snapshot.signals:
        if not signal.required:
            continue
        signal_age_ms = (current - parse_datetime(signal.sampled_at)).total_seconds() * 1_000.0
        if signal_age_ms > policy.max_signal_age_ms:
            stale_signals.append(signal.signal_id)
        elif signal_age_ms < -policy.future_tolerance_ms:
            future_signals.append(signal.signal_id)
    if stale_signals:
        reasons.append(
            _reason(
                "REQUIRED_SIGNAL_STALE",
                "UNKNOWN",
                "One or more required safety inputs are stale.",
                details={
                    "signal_ids": sorted(stale_signals),
                    "max_signal_age_ms": policy.max_signal_age_ms,
                },
            )
        )
    if future_signals:
        reasons.append(
            _reason(
                "REQUIRED_SIGNAL_TIME_INVALID",
                "UNKNOWN",
                "One or more required safety inputs are timestamped in the future.",
                details={"signal_ids": sorted(future_signals)},
            )
        )

    if measured_clock_quality is None:
        reasons.append(
            _reason(
                "CLOCK_MEASUREMENT_UNAVAILABLE",
                "UNKNOWN",
                "No trusted local clock measurement was supplied to the evaluator.",
            )
        )
    elif measured_clock_quality == "unknown":
        reasons.append(
            _reason(
                "CLOCK_QUALITY_UNKNOWN",
                "UNKNOWN",
                "Clock synchronization quality is unknown.",
            )
        )
    elif measured_clock_quality == "degraded":
        reasons.append(
            _reason(
                "CLOCK_QUALITY_DEGRADED",
                policy.degraded_clock_state,
                "Clock synchronization quality is degraded.",
            )
        )

    if snapshot.tracking_confidence is None and policy.require_human_distance:
        reasons.append(
            _reason(
                "TRACKING_CONFIDENCE_MISSING",
                "UNKNOWN",
                "Required human tracking lacks measured confidence.",
            )
        )
    elif snapshot.tracking_confidence is not None:
        if snapshot.tracking_confidence < policy.minimum_tracking_confidence:
            reasons.append(
                _reason(
                    "TRACKING_CONFIDENCE_LOW",
                    "UNKNOWN",
                    "Human or scene tracking confidence is below policy.",
                    details={
                        "confidence": snapshot.tracking_confidence,
                        "minimum": policy.minimum_tracking_confidence,
                    },
                )
            )

    distance = snapshot.human_robot_distance_m
    if distance is None and policy.require_human_distance:
        reasons.append(
            _reason(
                "HUMAN_ROBOT_DISTANCE_UNAVAILABLE",
                "UNKNOWN",
                "Human-to-robot distance is unavailable.",
                asset_ids=snapshot.asset_ids,
            )
        )
    elif distance is not None and distance < policy.unsafe_distance_m:
        reasons.append(
            _reason(
                "HUMAN_ROBOT_DISTANCE_UNSAFE",
                "UNSAFE",
                "Human-to-robot distance is below the unsafe threshold.",
                asset_ids=snapshot.asset_ids,
                details={
                    "distance_m": distance,
                    "threshold_m": policy.unsafe_distance_m,
                },
            )
        )
    elif distance is not None and distance < policy.warning_distance_m:
        reasons.append(
            _reason(
                "HUMAN_ROBOT_DISTANCE_WARNING",
                "WARNING",
                "Human-to-robot distance is below the warning threshold.",
                asset_ids=snapshot.asset_ids,
                details={
                    "distance_m": distance,
                    "threshold_m": policy.warning_distance_m,
                },
            )
        )

    for occupancy in sorted(
        snapshot.forbidden_zone_occupancies,
        key=lambda item: (item.zone_id, item.entity_id),
    ):
        reasons.append(
            _reason(
                "FORBIDDEN_ZONE_OCCUPIED",
                "UNSAFE",
                "An entity occupies a forbidden safety zone.",
                asset_ids=occupancy.asset_ids,
                details={
                    "zone_id": occupancy.zone_id,
                    "entity_id": occupancy.entity_id,
                },
            )
        )

    progress = snapshot.task_progress
    if progress is not None and progress.expected_step != progress.observed_step:
        reasons.append(
            _reason(
                "TASK_SEQUENCE_MISMATCH",
                policy.task_sequence_mismatch_state,
                "Observed Task state does not match the expected sequence step.",
                asset_ids=snapshot.asset_ids,
                details={
                    "expected_step": progress.expected_step,
                    "observed_step": progress.observed_step,
                },
            )
        )

    severities = {reason.severity for reason in reasons}
    if "UNSAFE" in severities:
        state = "UNSAFE"
    elif "UNKNOWN" in severities:
        state = "UNKNOWN"
    elif "WARNING" in severities:
        state = "WARNING"
    else:
        state = "SAFE"

    confidence_values = [
        signal.confidence
        for signal in snapshot.signals
        if signal.confidence is not None
    ]
    if snapshot.tracking_confidence is not None:
        confidence_values.append(snapshot.tracking_confidence)
    confidence = min(confidence_values, default=0.0)
    if state == "UNKNOWN":
        confidence = 0.0

    identity = {
        "snapshot": snapshot.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "assessed_at": isoformat_z(current),
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
    }
    assessment_id = f"safety-{_digest(identity)[:32]}"
    recommended_response = {
        "SAFE": "none",
        "WARNING": "operator_review",
        "UNSAFE": "task_pause_candidate",
        "UNKNOWN": "task_pause_candidate",
    }[state]
    return SafetyAssessmentV1(
        assessment_id=assessment_id,
        snapshot_id=snapshot.snapshot_id,
        source_kind=snapshot.source_kind,
        agent_id=snapshot.agent_id,
        run_id=snapshot.run_id,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        state=state,
        assessed_at=current,
        input_captured_at=captured,
        valid_until=valid_until,
        confidence=confidence,
        asset_ids=sorted(snapshot.asset_ids),
        reasons=reasons,
        evidence_refs=sorted(snapshot.evidence_refs),
        recommended_response=recommended_response,
        shadow_mode=True,
        command_emitted=False,
    )


class SafetyShadowCatalog:
    """Latest-only state plus append-only meaningful safety transitions."""

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
    def _decode(row: sqlite3.Row, *, include_event_id: bool = False) -> dict[str, Any]:
        result = json.loads(row["payload_json"])
        result["received_at"] = row["received_at"]
        if include_event_id:
            result["event_id"] = row["id"]
        return result

    @staticmethod
    def _prune_events(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM safety_events
            WHERE id IN (
                SELECT id FROM safety_events
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (MAX_SAFETY_EVENTS,),
        )

    def accept(
        self,
        assessment: SafetyAssessmentV1,
        *,
        received_at: datetime,
    ) -> dict[str, Any]:
        payload = assessment.model_dump(mode="json")
        payload_json = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        reason_signature = _transition_signature(assessment)
        received = parse_datetime(received_at)
        timestamp = isoformat_z(received)
        assessed_at = isoformat_z(assessment.assessed_at)
        future_skew_ms = (
            parse_datetime(assessment.assessed_at) - received
        ).total_seconds() * 1_000.0
        if future_skew_ms > MAX_ASSESSMENT_FUTURE_SKEW_MS:
            raise SafetyAssessmentInvalid(
                "assessment timestamp exceeds the Central future-skew tolerance"
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state FROM task_runs WHERE run_id = ?",
                (assessment.run_id,),
            ).fetchone()
            if run is None:
                raise SafetyAssessmentInvalid(
                    f"unknown Task Run: {assessment.run_id}"
                )
            if assessment.source_kind == "live" and run["state"] != "ACTIVE":
                raise SafetyAssessmentInvalid(
                    "live safety assessments require an ACTIVE Task Run"
                )

            assigned = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT asset_id FROM assignments WHERE run_id = ?",
                    (assessment.run_id,),
                ).fetchall()
            }
            missing_assets = sorted(set(assessment.asset_ids) - assigned)
            if missing_assets:
                raise SafetyAssessmentInvalid(
                    "safety assessment assets are not assigned to the Task Run: "
                    f"{missing_assets}"
                )
            if assessment.source_kind == "live":
                asset_rows = connection.execute(
                    """
                    SELECT assets.agent_id, assets.node_id
                    FROM assignments
                    JOIN assets ON assets.asset_id = assignments.asset_id
                    WHERE assignments.run_id = ?
                    """,
                    (assessment.run_id,),
                ).fetchall()
                allowed_agents = {
                    row["agent_id"]
                    for row in asset_rows
                    if row["agent_id"] is not None
                }
                node_ids = sorted(
                    {
                        row["node_id"]
                        for row in asset_rows
                        if row["node_id"] is not None
                    }
                )
                if node_ids:
                    placeholders = ",".join("?" for _ in node_ids)
                    allowed_agents.update(
                        row["agent_id"]
                        for row in connection.execute(
                            f"SELECT agent_id FROM node_agents "
                            f"WHERE node_id IN ({placeholders})",
                            node_ids,
                        ).fetchall()
                    )
                if not allowed_agents:
                    raise SafetyAssessmentInvalid(
                        "live Task Run assets do not identify an observing Agent"
                    )
                if assessment.agent_id not in allowed_agents:
                    raise SafetyAssessmentInvalid(
                        "safety assessment Agent is not bound to an assigned Task Run asset"
                    )

            duplicate = connection.execute(
                """
                SELECT payload_sha256, received_at
                FROM safety_latest WHERE assessment_id = ?
                UNION ALL
                SELECT payload_sha256, received_at
                FROM safety_events WHERE assessment_id = ?
                LIMIT 1
                """,
                (assessment.assessment_id, assessment.assessment_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["payload_sha256"] != payload_sha256:
                    raise SafetyAssessmentConflict(
                        "assessment_id was already used with different content"
                    )
                return {
                    **payload,
                    "received_at": duplicate["received_at"],
                    "transition_recorded": False,
                    "idempotent_replay": True,
                }

            previous = connection.execute(
                """
                SELECT * FROM safety_latest
                WHERE agent_id = ? AND run_id = ? AND evaluator_id = ?
                """,
                (assessment.agent_id, assessment.run_id, assessment.evaluator_id),
            ).fetchone()
            if previous is not None and parse_datetime(assessed_at) <= parse_datetime(
                previous["assessed_at"]
            ):
                raise SafetyAssessmentConflict(
                    "assessment is not newer than the current safety snapshot"
                )

            transition_recorded = (
                previous is None
                or previous["state"] != assessment.state
                or previous["reason_signature"] != reason_signature
            )
            if transition_recorded:
                connection.execute(
                    """
                    INSERT INTO safety_events (
                        assessment_id, agent_id, run_id, evaluator_id, state,
                        reason_signature, assessed_at, received_at,
                        payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.assessment_id,
                        assessment.agent_id,
                        assessment.run_id,
                        assessment.evaluator_id,
                        assessment.state,
                        reason_signature,
                        assessed_at,
                        timestamp,
                        payload_json,
                        payload_sha256,
                    ),
                )
                self._prune_events(connection)

            connection.execute(
                """
                INSERT INTO safety_latest (
                    agent_id, run_id, evaluator_id, assessment_id,
                    schema_version, evaluator_version, policy_id, policy_version,
                    state, reason_signature, assessed_at, input_captured_at,
                    valid_until, confidence, asset_ids_json, payload_json,
                    payload_sha256, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, run_id, evaluator_id) DO UPDATE SET
                    assessment_id = excluded.assessment_id,
                    schema_version = excluded.schema_version,
                    evaluator_version = excluded.evaluator_version,
                    policy_id = excluded.policy_id,
                    policy_version = excluded.policy_version,
                    state = excluded.state,
                    reason_signature = excluded.reason_signature,
                    assessed_at = excluded.assessed_at,
                    input_captured_at = excluded.input_captured_at,
                    valid_until = excluded.valid_until,
                    confidence = excluded.confidence,
                    asset_ids_json = excluded.asset_ids_json,
                    payload_json = excluded.payload_json,
                    payload_sha256 = excluded.payload_sha256,
                    received_at = excluded.received_at
                """,
                (
                    assessment.agent_id,
                    assessment.run_id,
                    assessment.evaluator_id,
                    assessment.assessment_id,
                    assessment.schema_version,
                    assessment.evaluator_version,
                    assessment.policy_id,
                    assessment.policy_version,
                    assessment.state,
                    reason_signature,
                    assessed_at,
                    isoformat_z(assessment.input_captured_at),
                    isoformat_z(assessment.valid_until),
                    assessment.confidence,
                    _canonical_json(assessment.asset_ids),
                    payload_json,
                    payload_sha256,
                    timestamp,
                ),
            )
            return {
                **payload,
                "received_at": timestamp,
                "transition_recorded": transition_recorded,
                "idempotent_replay": False,
            }

    def list_latest(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("safety latest limit must be between 1 and 500")
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            parameters.append(agent_id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM safety_latest{where} "
                "ORDER BY assessed_at DESC, agent_id, evaluator_id LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def list_events(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("safety event limit must be between 1 and 500")
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (("run_id", run_id), ("agent_id", agent_id), ("state", state)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM safety_events{where} ORDER BY id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode(row, include_event_id=True) for row in rows]


def _read_json(path: Path, *, maximum_bytes: int = 1_048_576) -> dict[str, Any]:
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"JSON input exceeds {maximum_bytes} bytes: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return value


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export or replay the command-free SANG safety Shadow Mode contract."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    contract_parser = subparsers.add_parser("export-contract")
    contract_parser.add_argument("--output", type=Path)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--snapshot", type=Path, required=True)
    evaluate_parser.add_argument("--policy", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path)
    evaluate_parser.add_argument("--evaluator-id", default="sang-reference-shadow")
    evaluate_parser.add_argument("--evaluator-version", default="0.1.0")
    evaluate_parser.add_argument("--assessed-at")
    evaluate_parser.add_argument(
        "--measured-clock-quality",
        choices=("synchronized", "degraded", "unknown"),
        help="trusted local monitor result; producer snapshot value is ignored",
    )

    args = parser.parse_args(argv)
    if args.command == "export-contract":
        _write_json(safety_contract_schema(), args.output)
        return 0

    snapshot = SafetySnapshotV1.model_validate(_read_json(args.snapshot))
    policy = SafetyPolicyV1.model_validate(_read_json(args.policy))
    assessed_at = (
        datetime.now(timezone.utc)
        if args.assessed_at is None
        else parse_datetime(args.assessed_at)
    )
    assessment = evaluate_snapshot(
        snapshot,
        policy,
        assessed_at=assessed_at,
        evaluator_id=args.evaluator_id,
        evaluator_version=args.evaluator_version,
        measured_clock_quality=args.measured_clock_quality,
    )
    _write_json(assessment.model_dump(mode="json"), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
