from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import fleet_control_plane.safety_shadow as safety_shadow_module
from fleet_control_plane.app import create_app
from fleet_control_plane.auth import AuthManager, ServiceIdentityCreate
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AgentRegistrationCreate,
    AssignmentCreate,
    AssetCreate,
    ObservedBinding,
    ProcessingNodeCreate,
    TaskDefinitionCreate,
    TaskRoleCreate,
    TaskRunCreate,
    TaskRunTransition,
)
from fleet_control_plane.safety_models import (
    ForbiddenZoneOccupancyV1,
    SafetyPolicyV1,
    SafetySignalV1,
    SafetySnapshotV1,
    TaskProgressV1,
    safety_contract_schema,
)
from fleet_control_plane.safety_shadow import (
    SafetyAssessmentConflict,
    SafetyAssessmentInvalid,
    SafetyShadowCatalog,
    evaluate_snapshot,
    main,
)
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def policy() -> SafetyPolicyV1:
    return SafetyPolicyV1(policy_id="rb5-lab-shadow", version=1)


def snapshot(
    *,
    snapshot_id: str = "snapshot-001",
    captured_at: datetime = NOW,
    sampled_at: datetime | None = None,
    distance_m: float | None = 1.5,
    clock_quality: str = "synchronized",
    tracking_confidence: float = 0.95,
    occupancies=None,
    progress=None,
) -> SafetySnapshotV1:
    return SafetySnapshotV1(
        snapshot_id=snapshot_id,
        source_kind="live",
        agent_id="processing-lab1",
        run_id="run-safety-01",
        captured_at=captured_at,
        ttl_ms=500,
        coordinate_frame="rb5-cell-world",
        clock_quality=clock_quality,
        asset_ids=["rb5-lab-robot"],
        signals=[
            SafetySignalV1(
                signal_id="robot-state",
                asset_id="rb5-lab-robot",
                sampled_at=sampled_at or captured_at,
                confidence=0.98,
            )
        ],
        human_robot_distance_m=distance_m,
        tracking_confidence=tracking_confidence,
        forbidden_zone_occupancies=occupancies or [],
        task_progress=progress,
    )


def active_store(tmp_path) -> FleetStore:
    store = FleetStore(tmp_path / "fleet.db")
    store.upsert(
        AgentHeartbeatV1.model_validate(
            {
                "schema_version": 1,
                "agent_id": "processing-lab1",
                "agent_name": "Processing Lab 1",
                "sw_version": "0.1.0",
                "sent_at": NOW.isoformat(),
                "storage": {
                    "disk_free_gb": 100.0,
                    "ram_free_gb": 20.0,
                    "cpu_pct": 10.0,
                },
                "datasets": {
                    "count": 0,
                    "total_episodes": 0,
                    "total_size_mb": 0.0,
                },
                "sync": {
                    "supported": False,
                    "state": "unsupported",
                    "pending": None,
                },
                "robots": [
                    {
                        "robot_id": "rb5-lab-eval",
                        "model": "rb5_850e",
                        "fsm_state": "READY",
                        "phase": "READY",
                        "sources_fresh": True,
                        "rates_ok": True,
                        "sources": [],
                        "recording": {
                            "active": False,
                            "episode_index": 0,
                            "episodes_total": 0,
                            "frames": 0,
                            "elapsed_s": 0.0,
                            "current_instruction": None,
                        },
                    }
                ],
            }
        ),
        received_at=NOW,
    )
    store.create_processing_node(
        ProcessingNodeCreate(
            node_id="processing-node-lab1",
            display_name="Processing Node Lab 1",
            agent_ids=["processing-lab1"],
        ),
        occurred_at=NOW,
    )
    store.create_asset(
        AssetCreate(
            asset_id="rb5-lab-robot",
            asset_type="robot",
            display_name="RB5 Lab Robot",
            node_id="processing-node-lab1",
            binding=ObservedBinding(
                agent_id="processing-lab1",
                robot_id="rb5-lab-eval",
            ),
            capabilities=["joint-state"],
        ),
        occurred_at=NOW,
    )
    store.create_task_definition(
        TaskDefinitionCreate(
            task_definition_id="safety-evaluation",
            version=1,
            display_name="Safety evaluation",
            roles=[
                TaskRoleCreate(
                    role="robot",
                    asset_type="robot",
                    access_mode="exclusive",
                )
            ],
        ),
        occurred_at=NOW,
    )
    store.create_task_run(
        TaskRunCreate(
            run_id="run-safety-01",
            task_definition_id="safety-evaluation",
            task_version=1,
            display_name="Safety evaluation run",
            assignments=[
                AssignmentCreate(
                    role="robot",
                    asset_id="rb5-lab-robot",
                    access_mode="exclusive",
                )
            ],
        ),
        occurred_at=NOW,
    )
    store.transition_task_run(
        "run-safety-01",
        TaskRunTransition(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )
    store.transition_task_run(
        "run-safety-01",
        TaskRunTransition(expected_revision=2, state="ACTIVE"),
        occurred_at=NOW,
    )
    return store


def evaluate(value: SafetySnapshotV1, *, assessed_at: datetime = NOW):
    return evaluate_snapshot(
        value,
        policy(),
        assessed_at=assessed_at,
        measured_clock_quality="synchronized",
    )


def test_missing_confidence_or_trusted_clock_fails_closed():
    value = snapshot().model_copy(
        update={
            "signals": [snapshot().signals[0].model_copy(update={"confidence": None})],
            "tracking_confidence": None,
        }
    )
    missing_confidence = evaluate_snapshot(
        value,
        policy(),
        assessed_at=NOW,
        measured_clock_quality="synchronized",
    )
    assert missing_confidence.state == "UNKNOWN"
    assert missing_confidence.confidence == 0.0
    assert {
        "REQUIRED_SIGNAL_CONFIDENCE_MISSING",
        "TRACKING_CONFIDENCE_MISSING",
    }.issubset({reason.code for reason in missing_confidence.reasons})

    producer_claim_only = evaluate_snapshot(snapshot(), policy(), assessed_at=NOW)
    assert producer_claim_only.state == "UNKNOWN"
    assert "CLOCK_MEASUREMENT_UNAVAILABLE" in {
        reason.code for reason in producer_claim_only.reasons
    }


def test_reference_evaluator_covers_all_states_without_commands():
    safe = evaluate(snapshot())
    warning = evaluate(snapshot(snapshot_id="snapshot-warning", distance_m=0.75))
    unsafe = evaluate(
        snapshot(
            snapshot_id="snapshot-unsafe",
            distance_m=0.2,
            occupancies=[
                ForbiddenZoneOccupancyV1(
                    zone_id="robot-envelope",
                    entity_id="person-01",
                    asset_ids=["rb5-lab-robot"],
                )
            ],
            progress=TaskProgressV1(
                expected_step="approach",
                observed_step="weld",
            ),
        )
    )
    unknown = evaluate(
        snapshot(
            snapshot_id="snapshot-unknown",
            captured_at=NOW - timedelta(seconds=1),
            sampled_at=NOW - timedelta(seconds=1),
        )
    )

    assert [item.state for item in (safe, warning, unsafe, unknown)] == [
        "SAFE",
        "WARNING",
        "UNSAFE",
        "UNKNOWN",
    ]
    assert safe.reasons == []
    assert warning.reasons[0].code == "HUMAN_ROBOT_DISTANCE_WARNING"
    assert {reason.code for reason in unsafe.reasons} == {
        "HUMAN_ROBOT_DISTANCE_UNSAFE",
        "FORBIDDEN_ZONE_OCCUPIED",
        "TASK_SEQUENCE_MISMATCH",
    }
    assert {reason.code for reason in unknown.reasons} == {
        "INPUT_STALE",
        "REQUIRED_SIGNAL_STALE",
    }
    assert all(item.shadow_mode is True for item in (safe, warning, unsafe, unknown))
    assert all(item.command_emitted is False for item in (safe, warning, unsafe, unknown))
    assert unsafe.recommended_response == "task_pause_candidate"


def test_unsafe_fact_has_precedence_over_unknown_input():
    result = evaluate(
        snapshot(
            captured_at=NOW - timedelta(seconds=1),
            sampled_at=NOW - timedelta(seconds=1),
            distance_m=0.2,
        )
    )
    assert result.state == "UNSAFE"
    assert {reason.severity for reason in result.reasons} == {"UNKNOWN", "UNSAFE"}


def test_evaluator_is_deterministic_for_same_input_and_time():
    first = evaluate(snapshot())
    second = evaluate(snapshot())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_snapshot_rejects_undeclared_assets_and_duplicate_signals():
    value = snapshot().model_dump(mode="json")
    value["signals"][0]["asset_id"] = "not-declared"
    with pytest.raises(ValueError, match="undeclared asset_ids"):
        SafetySnapshotV1.model_validate(value)

    value = snapshot().model_dump(mode="json")
    value["signals"].append(value["signals"][0])
    with pytest.raises(ValueError, match="signal_ids must be unique"):
        SafetySnapshotV1.model_validate(value)


def test_catalog_keeps_latest_and_only_meaningful_transitions(tmp_path):
    store = active_store(tmp_path)
    catalog = SafetyShadowCatalog(store.path)
    first = evaluate(snapshot())

    accepted = catalog.accept(first, received_at=NOW)
    replayed = catalog.accept(first, received_at=NOW + timedelta(milliseconds=10))
    assert accepted["transition_recorded"] is True
    assert accepted["idempotent_replay"] is False
    assert replayed["transition_recorded"] is False
    assert replayed["idempotent_replay"] is True
    assert replayed["received_at"] == accepted["received_at"]

    stable = evaluate(
        snapshot(
            snapshot_id="snapshot-002",
            captured_at=NOW + timedelta(milliseconds=100),
        ),
        assessed_at=NOW + timedelta(milliseconds=100),
    )
    stable_result = catalog.accept(
        stable,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert stable_result["transition_recorded"] is False
    assert len(catalog.list_events()) == 1

    warning = evaluate(
        snapshot(
            snapshot_id="snapshot-warning-001",
            captured_at=NOW + timedelta(milliseconds=150),
            distance_m=0.75,
        ),
        assessed_at=NOW + timedelta(milliseconds=150),
    )
    assert catalog.accept(
        warning,
        received_at=NOW + timedelta(milliseconds=150),
    )["transition_recorded"] is True
    warning_metric_update = evaluate(
        snapshot(
            snapshot_id="snapshot-warning-002",
            captured_at=NOW + timedelta(milliseconds=175),
            distance_m=0.8,
        ),
        assessed_at=NOW + timedelta(milliseconds=175),
    )
    assert catalog.accept(
        warning_metric_update,
        received_at=NOW + timedelta(milliseconds=175),
    )["transition_recorded"] is False

    unsafe = evaluate(
        snapshot(
            snapshot_id="snapshot-003",
            captured_at=NOW + timedelta(milliseconds=200),
            distance_m=0.2,
        ),
        assessed_at=NOW + timedelta(milliseconds=200),
    )
    assert catalog.accept(
        unsafe,
        received_at=NOW + timedelta(milliseconds=200),
    )["transition_recorded"] is True

    reopened = SafetyShadowCatalog(store.path)
    assert reopened.list_latest()[0]["state"] == "UNSAFE"
    assert [event["state"] for event in reopened.list_events()] == [
        "UNSAFE",
        "WARNING",
        "SAFE",
    ]

    with pytest.raises(SafetyAssessmentConflict, match="not newer"):
        catalog.accept(
            stable.model_copy(update={"assessment_id": "safety-out-of-order"}),
            received_at=NOW + timedelta(milliseconds=300),
        )

    with pytest.raises(SafetyAssessmentConflict, match="different content"):
        catalog.accept(
            unsafe.model_copy(update={"confidence": 0.5}),
            received_at=NOW + timedelta(milliseconds=400),
        )


def test_catalog_rejects_unassigned_assets_and_inactive_live_runs(tmp_path):
    store = active_store(tmp_path)
    catalog = SafetyShadowCatalog(store.path)
    result = evaluate(snapshot())
    with pytest.raises(SafetyAssessmentInvalid, match="not assigned"):
        catalog.accept(
            result.model_copy(update={"asset_ids": ["not-assigned"]}),
            received_at=NOW,
        )

    store.transition_task_run(
        "run-safety-01",
        TaskRunTransition(expected_revision=3, state="COMPLETE"),
        occurred_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(SafetyAssessmentInvalid, match="ACTIVE"):
        catalog.accept(
            result.model_copy(update={"assessment_id": "safety-inactive"}),
            received_at=NOW + timedelta(seconds=1),
        )
    replay = result.model_copy(
        update={
            "assessment_id": "safety-replay-complete",
            "source_kind": "replay",
            "assessed_at": NOW + timedelta(seconds=1),
        }
    )
    assert catalog.accept(
        replay,
        received_at=NOW + timedelta(seconds=1),
    )["transition_recorded"] is True


def test_catalog_rejects_future_time_and_wrong_task_agent(tmp_path):
    store = active_store(tmp_path)
    catalog = SafetyShadowCatalog(store.path)
    result = evaluate(snapshot())
    with pytest.raises(SafetyAssessmentInvalid, match="future-skew"):
        catalog.accept(
            result.model_copy(
                update={
                    "assessment_id": "safety-future",
                    "assessed_at": NOW + timedelta(seconds=3),
                }
            ),
            received_at=NOW,
        )
    with pytest.raises(SafetyAssessmentInvalid, match="not bound"):
        catalog.accept(
            result.model_copy(
                update={
                    "assessment_id": "safety-wrong-agent",
                    "agent_id": "processing-other",
                }
            ),
            received_at=NOW,
        )


def test_catalog_prunes_oldest_transition_events(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    catalog = SafetyShadowCatalog(store.path)
    monkeypatch.setattr(safety_shadow_module, "MAX_SAFETY_EVENTS", 2)
    distances = [1.5, 0.75, 1.5, 0.75]
    for index, distance in enumerate(distances):
        offset = timedelta(milliseconds=index * 50)
        result = evaluate(
            snapshot(
                snapshot_id=f"snapshot-prune-{index}",
                captured_at=NOW + offset,
                distance_m=distance,
            ),
            assessed_at=NOW + offset,
        )
        catalog.accept(result, received_at=NOW + offset)
    assert [event["state"] for event in catalog.list_events()] == [
        "WARNING",
        "SAFE",
    ]


def test_safety_api_accepts_and_reads_shadow_state(tmp_path):
    store = active_store(tmp_path)
    client = TestClient(create_app(store, now=lambda: NOW, auth_enabled=False))
    result = evaluate(snapshot())
    assert client.app.state.fleet_revision == 0

    response = client.post(
        "/api/v1/safety/assessments",
        json=result.model_dump(mode="json"),
    )
    assert response.status_code == 202, response.text
    assert response.json()["command_emitted"] is False
    assert client.app.state.fleet_revision == 1
    replay = client.post(
        "/api/v1/safety/assessments",
        json=result.model_dump(mode="json"),
    )
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert client.app.state.fleet_revision == 1
    stable = evaluate(
        snapshot(
            snapshot_id="snapshot-api-stable",
            captured_at=NOW + timedelta(milliseconds=100),
        ),
        assessed_at=NOW + timedelta(milliseconds=100),
    )
    assert client.post(
        "/api/v1/safety/assessments",
        json=stable.model_dump(mode="json"),
    ).json()["transition_recorded"] is False
    assert client.app.state.fleet_revision == 1
    assert client.get(
        "/api/v1/safety/latest",
        params={"run_id": "run-safety-01"},
    ).json()[0]["assessment_id"] == stable.assessment_id
    assert client.get(
        "/api/v1/safety/events",
        params={"state": "SAFE"},
    ).json()[0]["state"] == "SAFE"
    assert client.get(
        "/api/v1/safety/events",
        params={"state": "invalid"},
    ).status_code == 422


def test_safety_ingest_requires_operator_role_and_service_agent_scope(tmp_path):
    store = active_store(tmp_path)
    store.create_agent_registration(
        AgentRegistrationCreate(
            agent_id="processing-lab1",
            display_name="Processing Lab 1",
        ),
        occurred_at=NOW,
    )
    auth = AuthManager(store.path)
    token = "sang_pat_" + "s" * 32
    auth.create_service_identity(
        ServiceIdentityCreate(
            principal_id="unity-shadow",
            display_name="Unity Shadow Evaluator",
            role="operator",
            token_id="unity-shadow-token",
            agent_ids=["processing-lab1"],
        ),
        actor_id="test-admin",
        occurred_at=NOW,
        raw_token=token,
    )
    viewer_token = "sang_pat_" + "v" * 32
    auth.create_service_identity(
        ServiceIdentityCreate(
            principal_id="safety-viewer",
            display_name="Safety Viewer",
            role="viewer",
            token_id="safety-viewer-token",
        ),
        actor_id="test-admin",
        occurred_at=NOW,
        raw_token=viewer_token,
    )
    client = TestClient(create_app(store, now=lambda: NOW))
    headers = {"Authorization": f"Bearer {token}"}
    result = evaluate(snapshot())

    assert client.post(
        "/api/v1/safety/assessments",
        json=result.model_dump(mode="json"),
        headers=headers,
    ).status_code == 202
    denied = result.model_copy(
        update={
            "assessment_id": "safety-wrong-agent",
            "agent_id": "processing-other",
        }
    )
    response = client.post(
        "/api/v1/safety/assessments",
        json=denied.model_dump(mode="json"),
        headers=headers,
    )
    assert response.status_code == 403
    assert "not authorized for safety Agent" in response.json()["error"]
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
    assert client.get(
        "/api/v1/safety/latest",
        headers=viewer_headers,
    ).status_code == 200
    assert client.post(
        "/api/v1/safety/assessments",
        json=result.model_dump(mode="json"),
        headers=viewer_headers,
    ).status_code == 403


def test_safety_ingest_rejects_oversized_payload(tmp_path):
    store = active_store(tmp_path)
    client = TestClient(create_app(store, now=lambda: NOW, auth_enabled=False))
    response = client.post(
        "/api/v1/safety/assessments",
        content=b"x" * (256 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "safety assessment exceeds 256 KiB"


def test_contract_export_and_replay_cli(tmp_path):
    contract_path = tmp_path / "contract.json"
    snapshot_path = tmp_path / "snapshot.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "assessment.json"
    snapshot_path.write_text(
        json.dumps(snapshot().model_dump(mode="json")),
        encoding="utf-8",
    )
    policy_path.write_text(
        json.dumps(policy().model_dump(mode="json")),
        encoding="utf-8",
    )

    assert main(["export-contract", "--output", str(contract_path)]) == 0
    assert json.loads(contract_path.read_text(encoding="utf-8")) == safety_contract_schema()
    assert main(
        [
            "evaluate",
            "--snapshot",
            str(snapshot_path),
            "--policy",
            str(policy_path),
                "--assessed-at",
                "2026-08-27T12:00:00Z",
                "--measured-clock-quality",
                "synchronized",
                "--output",
            str(output_path),
        ]
    ) == 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["state"] == "SAFE"
    assert output["shadow_mode"] is True
    assert output["command_emitted"] is False


def test_checked_in_contract_matches_models():
    workspace = Path(__file__).resolve().parents[3]
    checked_in = json.loads(
        (workspace / "contracts/safety/shadow-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert checked_in == safety_contract_schema()
