from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.auth import AuthManager, UserCreate
from fleet_control_plane.backup import create_backup, restore_backup, verify_backup
from fleet_control_plane.commissioning_evidence import (
    G6B_REQUIRED_FAULT_SCENARIOS,
    G6B_REQUIRED_SAFETY_FUNCTIONS,
    G6B_REQUIRED_XR_LOSS_MODES,
    G6bCalibrationEvidenceV1,
    G6bCaseCreateV1,
    G6bEvidenceCatalog,
    G6bEvidenceConflict,
    G6bEvidenceInvalid,
    G6bEvidencePackageV1,
    G6bFaultResponseEvidenceV1,
    G6bLeaseTimingEvidenceV1,
    G6bOperatingEnvelopeEvidenceV1,
    G6bResourceSharingEvidenceV1,
    G6bReviewRequestV1,
    G6bRiskAssessmentEvidenceV1,
    G6bRoleAssignmentV1,
    G6bRoleAssignmentsEvidenceV1,
    G6bSafetyChainEvidenceV1,
    G6bStopMeasurementEvidenceV1,
    G6bStopSampleV1,
    G6bXrLossEvidenceV1,
)
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AssetCreate,
    ObservedBinding,
    ProcessingNodeCreate,
)
from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
VALID_UNTIL = NOW + timedelta(days=30)
LAB_ARTIFACT_PAYLOAD = b"SANG G6b laboratory evidence fixture\n"
LAB_ARTIFACT_SHA256 = hashlib.sha256(LAB_ARTIFACT_PAYLOAD).hexdigest()
LAB_ARTIFACT_ID = f"g6bart-{LAB_ARTIFACT_SHA256[:24]}"


def _heartbeat() -> AgentHeartbeatV1:
    return AgentHeartbeatV1.model_validate(
        {
            "schema_version": 3,
            "agent_id": "processing-lab1",
            "agent_name": "Processing Lab 1",
            "sw_version": "0.1.0",
            "sent_at": NOW,
            "boot_id": "019fce1c-a4b5-7ba2-b0d5-01ea4940926d",
            "sequence": 1,
            "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 10},
            "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
            "sync": {"supported": False, "state": "unsupported", "pending": None},
            "robots": [
                {
                    "robot_id": "rb5-lab1",
                    "model": "RB5-850E",
                    "runtime_kind": "physical",
                    "fsm_state": "READY",
                    "phase": "monitoring",
                    "sources_fresh": True,
                    "rates_ok": True,
                    "sources": [],
                    "fault": None,
                    "recording": {
                        "active": False,
                        "episode_index": 0,
                        "episodes_total": 0,
                        "frames": 0,
                        "elapsed_s": 0,
                        "current_instruction": None,
                    },
                }
            ],
        }
    )


def _artifact(name: str) -> dict:
    del name
    return {
        "uri": f"g6b-artifact://{LAB_ARTIFACT_ID}",
        "sha256": LAB_ARTIFACT_SHA256,
    }


def _evidence_base(name: str) -> dict:
    return {
        "evidence_id": name,
        "observed_at": NOW - timedelta(minutes=5),
        "valid_until": VALID_UNTIL,
        "observer_ids": ["lab-observer"],
        "artifact_refs": [_artifact(name)],
    }


def _case() -> G6bCaseCreateV1:
    return G6bCaseCreateV1(
        case_id="rb5-lab1-g6b",
        asset_id="rb5-asset",
        manifest_ref={
            "object_id": "rb5-manifest",
            "version": 1,
            "sha256": "a" * 64,
        },
        capability_refs=["bounded-move@1"],
        stop_scopes=[
            {
                "scope_id": "unloaded-low-speed",
                "tool_configuration_id": "no-tool",
                "payload_configuration_id": "unloaded",
                "speed_envelope_ref": "registry://speed/rb5-low",
                "pose_envelope_ref": "registry://workspace/rb5-cell",
                "required_stop_types": [
                    "controlled_stop",
                    "protective_stop",
                    "emergency_stop",
                ],
            }
        ],
        cell_frame_ref="registry://frames/boyd-706-cell",
        xr_control_in_scope=True,
        shared_resources_in_scope=False,
    )


def _full_package(*, revision: int = 1) -> G6bEvidencePackageV1:
    stop_measurements = [
        G6bStopMeasurementEvidenceV1(
            **_evidence_base(f"stop-{stop_type}"),
            stop_scope_id="unloaded-low-speed",
            stop_type=stop_type,
            maximum_permitted_stop_time_ms=500,
            maximum_permitted_stop_distance_mm=100,
            samples=[G6bStopSampleV1(stop_time_ms=150, stop_distance_mm=25)],
            test_passed=True,
        )
        for stop_type in ("controlled_stop", "protective_stop", "emergency_stop")
    ]
    fault_tests = [
        G6bFaultResponseEvidenceV1(
            **_evidence_base(f"fault-{scenario}"),
            scenario=scenario,
            expected_behavior="safe_state",
            observed_behavior="safe_state",
            measured_time_to_safe_state_ms=15,
            maximum_permitted_time_to_safe_state_ms=50,
            test_passed=True,
        )
        for scenario in sorted(G6B_REQUIRED_FAULT_SCENARIOS)
    ]
    return G6bEvidencePackageV1(
        case_id="rb5-lab1-g6b",
        revision=revision,
        risk_assessment=G6bRiskAssessmentEvidenceV1(
            **_evidence_base("risk-assessment"),
            protection_strategy="guarded_zone",
            risk_assessment_id="boyd-706-risk-v1",
            accepted_by_lab=True,
            person_excluded_during_motion=True,
        ),
        stop_measurements=stop_measurements,
        safety_chain=G6bSafetyChainEvidenceV1(
            **_evidence_base("safety-chain"),
            tested_functions=sorted(G6B_REQUIRED_SAFETY_FUNCTIONS),
            independent_of_sang=True,
            manual_local_reset=True,
            no_automatic_restart=True,
            test_passed=True,
        ),
        calibration=G6bCalibrationEvidenceV1(
            **_evidence_base("cell-calibration"),
            cell_frame_ref="registry://frames/boyd-706-cell",
            workspace_ref="registry://workspace/rb5-cell",
            calibration_method="surveyed fiducial fixture",
            error_samples_mm=[0.5, 0.7, 0.6],
            maximum_permitted_error_mm=1.0,
            test_passed=True,
        ),
        fault_response_tests=fault_tests,
        lease_timing=G6bLeaseTimingEvidenceV1(
            **_evidence_base("lease-timing"),
            lease_expiry_ms=1000,
            renewal_margin_ms=250,
            measured_time_to_safe_state_ms=20,
            maximum_permitted_time_to_safe_state_ms=50,
            test_passed=True,
        ),
        role_assignments=G6bRoleAssignmentsEvidenceV1(
            **_evidence_base("named-roles"),
            assignments=[
                G6bRoleAssignmentV1(role="edge_operator", principal_id="edge-user"),
                G6bRoleAssignmentV1(role="manager", principal_id="manager-user"),
                G6bRoleAssignmentV1(
                    role="safety_officer",
                    principal_id="safety-user",
                ),
            ],
            release_key_custody_documented=True,
        ),
        operating_envelope=G6bOperatingEnvelopeEvidenceV1(
            **_evidence_base("operating-envelope"),
            capability_refs=["bounded-move@1"],
            workspace_ref="registry://workspace/rb5-cell",
            speed_envelope_refs=["registry://speed/rb5-low"],
            pose_envelope_refs=["registry://workspace/rb5-cell"],
            tool_configuration_ids=["no-tool"],
            payload_configuration_ids=["unloaded"],
            test_passed=True,
        ),
        xr_loss_response=G6bXrLossEvidenceV1(
            **_evidence_base("xr-loss"),
            xr_control_in_scope=True,
            tested_loss_modes=sorted(G6B_REQUIRED_XR_LOSS_MODES),
            intervention_inhibited_on_loss=True,
            manual_reauthorization_required=True,
            physical_estop_independent=True,
            xr_control_prohibited_when_unqualified=True,
            test_passed=True,
        ),
        resource_sharing=G6bResourceSharingEvidenceV1(
            **_evidence_base("resource-sharing"),
            sharing_permitted=False,
            exclusive_reservations_required=True,
            test_passed=True,
        ),
    )


def _setup(tmp_path) -> tuple[FleetStore, G6bEvidenceCatalog]:
    store = FleetStore(tmp_path / "fleet.db")
    store.upsert(_heartbeat(), received_at=NOW)
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
            asset_id="rb5-asset",
            asset_type="robot",
            display_name="RB5 Lab 1",
            runtime_kind="physical",
            node_id="processing-node-lab1",
            binding=ObservedBinding(
                agent_id="processing-lab1",
                robot_id="rb5-lab1",
            ),
            capabilities=["telemetry"],
        ),
        occurred_at=NOW,
    )
    auth = AuthManager(store.path)
    auth.bootstrap_admin(
        principal_id="manager-user",
        display_name="Manager User",
        password="manager-password-123",
        occurred_at=NOW,
    )
    auth.create_user(
        UserCreate(
            principal_id="safety-user",
            display_name="Safety Officer",
            role="admin",
            password="safety-password-123",
        ),
        actor_id="manager-user",
        occurred_at=NOW,
    )
    auth.create_user(
        UserCreate(
            principal_id="edge-user",
            display_name="Edge Operator",
            role="operator",
            password="operator-password-123",
        ),
        actor_id="manager-user",
        occurred_at=NOW,
    )
    catalog = G6bEvidenceCatalog(
        store.path,
        FilesystemObjectStore(tmp_path / "objects"),
    )
    asyncio.run(
        catalog.import_supporting_artifact(
            LAB_ARTIFACT_PAYLOAD,
            source_file_name="lab-evidence.txt",
            media_type="text/plain",
            expected_sha256=LAB_ARTIFACT_SHA256,
            uploaded_by="manager-user",
            occurred_at=NOW,
        )
    )
    return store, catalog


def _create_case(catalog: G6bEvidenceCatalog) -> dict:
    return catalog.create_case(
        _case(),
        created_by="manager-user",
        occurred_at=NOW,
    )


def _review(
    catalog: G6bEvidenceCatalog,
    submission: dict,
    *,
    reviewer_id: str,
    role: str,
    decision: str = "accept",
) -> dict:
    return catalog.review(
        submission["submission_id"],
        G6bReviewRequestV1(
            reviewer_id=reviewer_id,
            review_role=role,
            decision=decision,
            expected_submission_sha256=submission["payload_sha256"],
            rationale=f"{role} reviewed the immutable lab evidence",
        ),
        occurred_at=NOW,
    )


def test_missing_and_partial_evidence_remain_unverified(tmp_path):
    _, catalog = _setup(tmp_path)
    _create_case(catalog)

    empty = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert empty.lab_evidence_status == "UNVERIFIED"
    assert empty.evidence_complete is False
    assert len(empty.blockers) == 11
    assert empty.g7_authorized is False

    partial = G6bEvidencePackageV1(
        case_id="rb5-lab1-g6b",
        revision=1,
        risk_assessment=G6bRiskAssessmentEvidenceV1(
            **_evidence_base("risk-only"),
            protection_strategy="guarded_zone",
            risk_assessment_id="risk-only-v1",
            accepted_by_lab=True,
            person_excluded_during_motion=True,
        ),
    )
    asyncio.run(catalog.submit(partial, submitted_by="manager-user", occurred_at=NOW))

    report = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert report.lab_evidence_status == "UNVERIFIED"
    assert report.evidence_complete is False
    assert report.command_sink_present is False
    assert report.physical_motion_enabled is False
    assert report.process_output_enabled is False
    assert report.can_create_physical_manifest is False
    assert report.g7_authorized is False


def test_complete_evidence_requires_two_matching_reviews_and_never_authorizes_g7(tmp_path):
    _, catalog = _setup(tmp_path)
    case = _create_case(catalog)
    assert case["agent_id"] == "processing-lab1"
    package = _full_package()
    submission = asyncio.run(
        catalog.submit(package, submitted_by="manager-user", occurred_at=NOW)
    )
    assert asyncio.run(
        catalog.submit(package, submitted_by="manager-user", occurred_at=NOW)
    ) == submission

    ready = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert ready.evidence_complete is True
    assert ready.lab_evidence_status == "READY_FOR_REVIEW"
    assert len(ready.blockers) == 2
    assert catalog.open_submission(submission["submission_id"]).read_bytes()

    _review(
        catalog,
        submission,
        reviewer_id="manager-user",
        role="manager",
    )
    one_review = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert one_review.lab_evidence_status == "READY_FOR_REVIEW"

    _review(
        catalog,
        submission,
        reviewer_id="safety-user",
        role="safety_officer",
    )
    reviewed = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert reviewed.lab_evidence_status == "REVIEWED"
    assert reviewed.blockers == []
    assert reviewed.g7_authorized is False
    assert reviewed.can_create_physical_manifest is False
    assert reviewed.command_sink_present is False
    assert reviewed.physical_motion_enabled is False
    assert reviewed.process_output_enabled is False


def test_reviews_are_digest_bound_role_bound_and_append_only(tmp_path):
    _, catalog = _setup(tmp_path)
    _create_case(catalog)
    submission = asyncio.run(
        catalog.submit(_full_package(), submitted_by="manager-user", occurred_at=NOW)
    )

    stale = G6bReviewRequestV1(
        reviewer_id="manager-user",
        review_role="manager",
        decision="accept",
        expected_submission_sha256="c" * 64,
        rationale="stale evidence",
    )
    with pytest.raises(G6bEvidenceConflict, match="stale"):
        catalog.review(submission["submission_id"], stale, occurred_at=NOW)

    wrong_role = G6bReviewRequestV1(
        reviewer_id="safety-user",
        review_role="manager",
        decision="accept",
        expected_submission_sha256=submission["payload_sha256"],
        rationale="wrong named role",
    )
    with pytest.raises(G6bEvidenceInvalid, match="named role"):
        catalog.review(submission["submission_id"], wrong_role, occurred_at=NOW)

    first = _review(
        catalog,
        submission,
        reviewer_id="manager-user",
        role="manager",
        decision="reject",
    )
    assert first["decision"] == "reject"
    assert _review(
        catalog,
        submission,
        reviewer_id="manager-user",
        role="manager",
        decision="reject",
    ) == first
    report = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert report.lab_evidence_status == "UNVERIFIED"

    with pytest.raises(G6bEvidenceConflict, match="different decision"):
        _review(
            catalog,
            submission,
            reviewer_id="manager-user",
            role="manager",
            decision="accept",
        )


def test_expired_or_out_of_limit_measurement_cannot_be_accepted(tmp_path):
    _, catalog = _setup(tmp_path)
    _create_case(catalog)
    package = _full_package()
    bad_stop = package.stop_measurements[0].model_copy(
        update={
            "samples": [G6bStopSampleV1(stop_time_ms=900, stop_distance_mm=25)]
        }
    )
    package = package.model_copy(
        update={"stop_measurements": [bad_stop, *package.stop_measurements[1:]]}
    )
    submission = asyncio.run(
        catalog.submit(package, submitted_by="manager-user", occurred_at=NOW)
    )

    report = catalog.report("rb5-lab1-g6b", generated_at=NOW)
    assert report.lab_evidence_status == "UNVERIFIED"
    assert next(item for item in report.checks if item.check_id == "stop-measurements").passed is False
    with pytest.raises(G6bEvidenceInvalid, match="cannot be accepted"):
        _review(
            catalog,
            submission,
            reviewer_id="manager-user",
            role="manager",
        )


def test_missing_or_corrupt_supporting_artifact_keeps_evidence_unverified(tmp_path):
    _, catalog = _setup(tmp_path)
    _create_case(catalog)
    package = _full_package()
    missing_risk_payload = package.risk_assessment.model_dump(mode="json")
    missing_risk_payload["artifact_refs"] = [
        {
            "uri": "g6b-artifact://g6bart-missing",
            "sha256": "d" * 64,
        }
    ]
    missing_risk = G6bRiskAssessmentEvidenceV1.model_validate(
        missing_risk_payload
    )
    package = package.model_copy(update={"risk_assessment": missing_risk})
    submission = asyncio.run(
        catalog.submit(package, submitted_by="manager-user", occurred_at=NOW)
    )

    report = catalog.report("rb5-lab1-g6b", generated_at=NOW)

    artifact_check = next(
        item for item in report.checks if item.check_id == "supporting-artifacts"
    )
    assert artifact_check.passed is False
    assert report.lab_evidence_status == "UNVERIFIED"
    with pytest.raises(G6bEvidenceInvalid, match="cannot be accepted"):
        _review(
            catalog,
            submission,
            reviewer_id="manager-user",
            role="manager",
        )


def test_damaged_supporting_artifact_revokes_completeness(tmp_path):
    _, catalog = _setup(tmp_path)
    _create_case(catalog)
    asyncio.run(
        catalog.submit(_full_package(), submitted_by="manager-user", occurred_at=NOW)
    )
    path = catalog.open_supporting_artifact(LAB_ARTIFACT_ID)
    path.write_bytes(b"damaged")

    report = catalog.report("rb5-lab1-g6b", generated_at=NOW)

    assert report.lab_evidence_status == "UNVERIFIED"
    assert next(
        item for item in report.checks if item.check_id == "supporting-artifacts"
    ).passed is False


def test_multi_scope_case_requires_every_speed_pose_and_stop_measurement(tmp_path):
    _, catalog = _setup(tmp_path)
    base_case = _case()
    second_scope = base_case.stop_scopes[0].model_copy(
        update={
            "scope_id": "loaded-medium-speed",
            "tool_configuration_id": "torch-tool",
            "payload_configuration_id": "loaded",
            "speed_envelope_ref": "registry://speed/rb5-medium",
            "pose_envelope_ref": "registry://workspace/rb5-loaded",
        }
    )
    case = base_case.model_copy(
        update={
            "case_id": "rb5-multi-scope-g6b",
            "stop_scopes": [base_case.stop_scopes[0], second_scope],
        }
    )
    catalog.create_case(case, created_by="manager-user", occurred_at=NOW)
    package = _full_package().model_copy(
        update={"case_id": "rb5-multi-scope-g6b"}
    )
    asyncio.run(catalog.submit(package, submitted_by="manager-user", occurred_at=NOW))

    report = catalog.report("rb5-multi-scope-g6b", generated_at=NOW)

    assert report.lab_evidence_status == "UNVERIFIED"
    checks = {item.check_id: item.passed for item in report.checks}
    assert checks["stop-measurements"] is False
    assert checks["operating-envelope"] is False


def test_case_is_idempotent_and_rejects_nonphysical_or_changed_scope(tmp_path):
    store, catalog = _setup(tmp_path)
    first = _create_case(catalog)
    assert _create_case(catalog) == first
    changed = _case().model_copy(update={"xr_control_in_scope": False})
    with pytest.raises(G6bEvidenceConflict, match="different scope"):
        catalog.create_case(changed, created_by="manager-user", occurred_at=NOW)

    store.create_asset(
        AssetCreate(
            asset_id="sim-asset",
            asset_type="robot",
            display_name="Simulation Robot",
            runtime_kind="simulation",
            capabilities=["telemetry"],
        ),
        occurred_at=NOW,
    )
    sim_case = _case().model_copy(
        update={"case_id": "sim-g6b", "asset_id": "sim-asset"}
    )
    with pytest.raises(G6bEvidenceInvalid, match="active physical"):
        catalog.create_case(sim_case, created_by="manager-user", occurred_at=NOW)


def test_g6b_api_exposes_evidence_only_and_no_authorization_route(tmp_path):
    store, _ = _setup(tmp_path)
    object_store = FilesystemObjectStore(tmp_path / "objects")
    app = create_app(
        store,
        auth_enabled=False,
        object_store=object_store,
        now=lambda: NOW,
    )
    client = TestClient(app)
    uploaded = client.post(
        "/api/v1/commissioning/g6b/artifacts",
        params={
            "source_file_name": "lab-evidence.txt",
            "sha256": LAB_ARTIFACT_SHA256,
        },
        content=LAB_ARTIFACT_PAYLOAD,
        headers={"Content-Type": "text/plain"},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["artifact_id"] == LAB_ARTIFACT_ID
    case_response = client.post(
        "/api/v1/commissioning/g6b/cases",
        json=_case().model_dump(mode="json"),
    )
    assert case_response.status_code == 201, case_response.text
    submission_response = client.post(
        "/api/v1/commissioning/g6b/cases/rb5-lab1-g6b/submissions",
        json=_full_package().model_dump(mode="json"),
    )
    assert submission_response.status_code == 201, submission_response.text
    submission = submission_response.json()
    artifact = client.get(
        f"/api/v1/commissioning/g6b/submissions/{submission['submission_id']}/artifact"
    )
    assert artifact.status_code == 200
    report = client.get(
        "/api/v1/commissioning/g6b/cases/rb5-lab1-g6b/report"
    ).json()
    assert report["lab_evidence_status"] == "READY_FOR_REVIEW"
    assert report["g7_authorized"] is False

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    g6b_paths = {path.casefold() for path in paths if "/commissioning/g6b/" in path}
    assert not any(
        token in path
        for path in g6b_paths
        for token in ("enable", "authorize", "command", "execute", "manifest")
    )


def test_g6b_api_requires_object_store(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    client = TestClient(create_app(store, auth_enabled=False))

    response = client.get("/api/v1/commissioning/g6b/cases")

    assert response.status_code == 503
    assert "Object Store" in response.json()["error"]


def test_g6b_evidence_survives_verified_backup_and_restore(tmp_path):
    store, catalog = _setup(tmp_path)
    _create_case(catalog)
    submission = asyncio.run(
        catalog.submit(_full_package(), submitted_by="manager-user", occurred_at=NOW)
    )
    backup = tmp_path / "backup"

    manifest = asyncio.run(
        create_backup(
            db_path=store.path,
            object_store_root=tmp_path / "objects",
            output=backup,
        )
    )

    assert any(
        item["sha256"] == submission["payload_sha256"]
        for item in manifest["objects"]
    )
    assert verify_backup(backup)["passed"] is True
    restored_db = tmp_path / "restored/fleet.db"
    restored_objects = tmp_path / "restored/objects"
    asyncio.run(
        restore_backup(
            backup=backup,
            db_path=restored_db,
            object_store_root=restored_objects,
        )
    )
    restored = G6bEvidenceCatalog(
        restored_db,
        FilesystemObjectStore(restored_objects),
    )
    report = restored.report("rb5-lab1-g6b", generated_at=NOW)
    assert report.evidence_complete is True
    assert report.lab_evidence_status == "READY_FOR_REVIEW"
