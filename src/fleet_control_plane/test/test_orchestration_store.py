from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import UUID

import pytest
from pydantic import ValidationError

from fleet_control_plane.orchestration_models import (
    CapabilityProfileV1,
    ClockMeasurementV2,
    DeviceManifestV1,
    RuntimeBindingV1,
    WorkflowDefinitionV2,
)
from fleet_control_plane.orchestration_runtime_models import (
    GenericRegistryObjectV1,
    OrchestrationRunCreateV1,
    OrchestrationRunTransitionV1,
    ProducerBindingCreateV1,
    SafetyPolicyV2,
)
from fleet_control_plane.orchestration_store import (
    OrchestrationConflict,
    OrchestrationInvalid,
    OrchestrationStore,
)
from fleet_control_plane.safety_v2 import SafetyEvidenceIngressV2, SafetyV2Catalog
from fleet_control_plane.control_authority import (
    ControlAuthority,
    ControlAuthorityError,
    XrSessionUpdateV1,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64


def _profile() -> CapabilityProfileV1:
    return CapabilityProfileV1.model_validate(
        {
            "profile_id": "generic-cell-assets",
            "version": 1,
            "kind_tags": ["simulation"],
            "capabilities": [
                {
                    "capability_id": "execute-plan",
                    "version": 1,
                    "effect": "actuation",
                    "parameter_schema_ref": "contracts://execute-plan.v1",
                    "required_frames": ["base", "tool"],
                    "required_units": ["joint-position"],
                    "required_limits": ["joint-position"],
                    "control_envelope_schema_ref": "contracts://envelope.v1",
                    "completion_contract_ref": "contracts://complete.v1",
                    "cancellation_contract_ref": "contracts://cancel.v1",
                    "recovery_contract_ref": "contracts://recover.v1",
                    "evidence_contract_ref": "contracts://evidence.v1",
                    "allowed_execution_modes": ["SIMULATION", "HIL"],
                    "max_clock_uncertainty_ms": 0,
                    "max_demand_age_ms": 0,
                    "on_lease_expiry": "safe_state",
                    "on_agent_restart_with_motion_in_flight": "safe_state",
                },
                {
                    "capability_id": "record-video",
                    "version": 1,
                    "effect": "observation",
                    "parameter_schema_ref": "contracts://record-video.v1",
                    "allowed_execution_modes": [
                        "OBSERVE_ONLY",
                        "SIMULATION",
                        "REPLAY",
                        "SHADOW",
                        "HIL",
                    ],
                },
            ],
        }
    )


def _manifest(asset_id: str, capability: str) -> DeviceManifestV1:
    return DeviceManifestV1.model_validate(
        {
            "manifest_id": f"{asset_id}-manifest",
            "version": 1,
            "profile_ref": {"profile_id": "generic-cell-assets", "version": 1},
            "vendor": {"make": "fixture", "model": "generic"},
            "capability_refs": [capability],
            "frames": {
                "mappings": {"base": "base", "tool": "tool"},
                "world_calibration_required": False,
            },
            "units": {"mappings": {"joint-position": "rad"}},
            "limits_ref": {
                "object_id": f"{asset_id}-limits",
                "version": 1,
                "sha256": SHA_A,
            },
            "observation_contract": [],
            "execution_contract": {
                "agent_api": "sang_control_msgs/action/ExecuteSkill@1",
                "supported_modes": ["SIMULATION"],
                "command_sink_present": False,
            },
            "local_fail_behavior_ref": "contracts://fail/output-disconnected.v1",
        }
    )


def _generic(object_id: str, payload: dict | None = None) -> GenericRegistryObjectV1:
    return GenericRegistryObjectV1(
        object_id=object_id,
        version=1,
        payload=payload or {"fixture": True},
        approval_state="approved",
    )


def _workflow(
    camera_only: bool = False, *, zone_id: str = "cell-view"
) -> WorkflowDefinitionV2:
    roles = []
    steps = []
    if not camera_only:
        roles.append(
            {
                "role": "handler",
                "cardinality": "1",
                "selector": {"requires": ["execute-plan@1"]},
                "access": "exclusive",
            }
        )
        steps.append(
            {
                "step_id": "move",
                "kind": "action",
                "role": "handler",
                "capability": "execute-plan@1",
                "parameters": {"plan_ref": "artifact://plans/fixture@1"},
                "timeout_s": 10,
            }
        )
    roles.append(
        {
            "role": "views",
            "cardinality": "1..n",
            "selector": {"requires": ["record-video@1"], "zone_id": zone_id},
            "access": "shared_read",
        }
    )
    steps.append(
        {
            "step_id": "capture",
            "kind": "action",
            "role": "views",
            "capability": "record-video@1",
            "parameters": {},
            "timeout_s": 10,
            "fan_out": "all_bound_assets",
            "join": "all",
        }
    )
    return WorkflowDefinitionV2.model_validate(
        {
            "workflow_id": (
                f"camera-only-{zone_id}" if camera_only else f"move-and-capture-{zone_id}"
            ),
            "version": 1,
            "roles": roles,
            "steps": steps,
        }
    )


def _register_fixture(
    store: OrchestrationStore,
    run_id: str,
    *,
    camera_only: bool = False,
    camera_ids: tuple[str, ...] = ("camera-01",),
    zone_id: str = "cell-view",
) -> tuple[WorkflowDefinitionV2, RuntimeBindingV1]:
    profile = _profile()
    store.register_model(profile, occurred_at=NOW)
    workflow = _workflow(camera_only, zone_id=zone_id)
    store.register_model(workflow, occurred_at=NOW)

    assets = []
    roles = []
    if not camera_only:
        manifest = store.register_model(_manifest("arm-01", "execute-plan@1"), occurred_at=NOW)
        connection = store.register_generic(
            "connection_snapshot", _generic("arm-01-connection"), occurred_at=NOW
        )
        assets.append(
            {
                "asset_id": "arm-01",
                "runtime_class": "simulation",
                "manifest_ref": {
                    "manifest_id": "arm-01-manifest",
                    "version": 1,
                    "sha256": manifest["payload_sha256"],
                },
                "connection_ref": {
                    "object_id": "arm-01-connection",
                    "version": 1,
                    "sha256": connection["payload_sha256"],
                },
                "capabilities": ["execute-plan@1"],
                "supported_modes": ["SIMULATION"],
            }
        )
        roles.append({"role": "handler", "asset_ids": ["arm-01"]})

    for camera_id in camera_ids:
        manifest = store.register_model(
            _manifest(camera_id, "record-video@1"), occurred_at=NOW
        )
        connection = store.register_generic(
            "connection_snapshot", _generic(f"{camera_id}-connection"), occurred_at=NOW
        )
        assets.append(
            {
                "asset_id": camera_id,
                "runtime_class": "simulation",
                "manifest_ref": {
                    "manifest_id": f"{camera_id}-manifest",
                    "version": 1,
                    "sha256": manifest["payload_sha256"],
                },
                "connection_ref": {
                    "object_id": f"{camera_id}-connection",
                    "version": 1,
                    "sha256": connection["payload_sha256"],
                },
                "capabilities": ["record-video@1"],
                "supported_modes": ["SIMULATION"],
            }
        )
    roles.append({"role": "views", "asset_ids": list(camera_ids)})
    binding = RuntimeBindingV1.model_validate(
        {
            "binding_id": f"binding-{run_id}",
            "run_id": run_id,
            "workflow_ref": {
                "workflow_id": workflow.workflow_id,
                "version": workflow.version,
            },
            "execution_mode": "SIMULATION",
            "assets": assets,
            "role_bindings": roles,
        }
    )
    store.register_model(binding, occurred_at=NOW)
    return workflow, binding


def _create_run(
    store: OrchestrationStore,
    run_id: str,
    workflow: WorkflowDefinitionV2,
    binding: RuntimeBindingV1,
    *,
    workcell_id: str,
):
    return store.create_run(
        OrchestrationRunCreateV1(
            run_id=run_id,
            display_name=run_id,
            workcell_id=workcell_id,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            binding_id=binding.binding_id,
            execution_mode="SIMULATION",
            orchestrator_id="orchestrator-01",
        ),
        occurred_at=NOW,
    )


def test_registry_is_immutable_and_idempotent(tmp_path):
    store = OrchestrationStore(tmp_path / "fleet.db")
    first = store.register_model(_profile(), occurred_at=NOW)
    replay = store.register_model(_profile(), occurred_at=NOW)
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True

    changed = _profile().model_copy(update={"kind_tags": ["changed"]})
    with pytest.raises(OrchestrationConflict, match="immutable"):
        store.register_model(changed, occurred_at=NOW)


def test_run_contract_is_pinned_and_whole_workcell_is_exclusive(tmp_path):
    store = OrchestrationStore(tmp_path / "fleet.db")
    workflow_a, binding_a = _register_fixture(store, "run-a")
    workflow_b, binding_b = _register_fixture(store, "run-b")
    created = _create_run(store, "run-a", workflow_a, binding_a, workcell_id="cell-01")
    assert created["state"] == "DRAFT"
    ready = store.transition_run(
        "run-a",
        OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )
    assert ready["state"] == "READY"
    assert any(item["resource_kind"] == "workcell" for item in ready["reservations"])

    _create_run(store, "run-b", workflow_b, binding_b, workcell_id="cell-01")
    with pytest.raises(OrchestrationConflict, match="workcell cell-01"):
        store.transition_run(
            "run-b",
            OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
            occurred_at=NOW,
        )


def test_shared_read_camera_can_be_bound_by_disjoint_workcells(tmp_path):
    store = OrchestrationStore(tmp_path / "fleet.db")
    workflow_a, binding_a = _register_fixture(
        store,
        "read-a",
        camera_only=True,
        camera_ids=("shared-camera",),
        zone_id="view-a",
    )
    workflow_b, binding_b = _register_fixture(
        store,
        "read-b",
        camera_only=True,
        camera_ids=("shared-camera",),
        zone_id="view-b",
    )
    _create_run(store, "read-a", workflow_a, binding_a, workcell_id="cell-a")
    _create_run(store, "read-b", workflow_b, binding_b, workcell_id="cell-b")
    store.transition_run(
        "read-a",
        OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )
    second = store.transition_run(
        "read-b",
        OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )
    camera = next(
        item for item in second["reservations"] if item["resource_id"] == "shared-camera"
    )
    assert camera["access_mode"] == "shared_read"


def test_zone_is_exclusive_even_when_bound_assets_are_shared_read(tmp_path):
    store = OrchestrationStore(tmp_path / "fleet.db")
    workflow_a, binding_a = _register_fixture(
        store,
        "zone-a",
        camera_only=True,
        camera_ids=("camera-a",),
        zone_id="shared-protective-zone",
    )
    workflow_b, binding_b = _register_fixture(
        store,
        "zone-b",
        camera_only=True,
        camera_ids=("camera-b",),
        zone_id="shared-protective-zone",
    )
    _create_run(store, "zone-a", workflow_a, binding_a, workcell_id="cell-a")
    _create_run(store, "zone-b", workflow_b, binding_b, workcell_id="cell-b")
    store.transition_run(
        "zone-a",
        OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )

    with pytest.raises(OrchestrationConflict, match="zone shared-protective-zone"):
        store.transition_run(
            "zone-b",
            OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
            occurred_at=NOW,
        )


def test_provenance_is_derived_from_run_not_the_producer(tmp_path):
    store = OrchestrationStore(tmp_path / "fleet.db")
    workflow, binding = _register_fixture(store, "run-provenance")
    _create_run(store, "run-provenance", workflow, binding, workcell_id="cell-01")
    store.bind_producer(
        "run-provenance",
        ProducerBindingCreateV1(
            producer_id="unity-observer", asset_ids=["arm-01", "camera-01"]
        ),
        occurred_at=NOW,
    )
    provenance = store.derive_provenance(
        "run-provenance", "unity-observer", ["arm-01"]
    )
    assert provenance.source_kind == "synthetic"
    assert provenance.execution_mode == "SIMULATION"

    with pytest.raises(OrchestrationInvalid, match="not bound"):
        store.derive_provenance("run-provenance", "unknown", ["arm-01"])


def test_physical_modes_are_unavailable_before_g7():
    with pytest.raises(ValueError, match="before G7"):
        OrchestrationRunCreateV1(
            run_id="physical-run",
            display_name="Physical",
            workcell_id="cell-01",
            workflow_id="workflow",
            workflow_version=1,
            binding_id="binding",
            execution_mode="PHYSICAL_AUTO",
            orchestrator_id="orchestrator-01",
        )


def _active_safety_fixture(tmp_path):
    database = tmp_path / "fleet.db"
    store = OrchestrationStore(database)
    workflow, binding = _register_fixture(store, "safety-run")
    _create_run(store, "safety-run", workflow, binding, workcell_id="cell-safety")
    store.bind_producer(
        "safety-run",
        ProducerBindingCreateV1(
            producer_id="unity-observer", asset_ids=["arm-01"]
        ),
        occurred_at=NOW,
    )
    store.transition_run(
        "safety-run",
        OrchestrationRunTransitionV1(expected_revision=1, state="READY"),
        occurred_at=NOW,
    )
    store.transition_run(
        "safety-run",
        OrchestrationRunTransitionV1(expected_revision=2, state="ACTIVE"),
        occurred_at=NOW,
    )
    policy = SafetyPolicyV2(
        policy_id="cell-safety-policy",
        version=1,
        execution_modes=["SIMULATION"],
        max_snapshot_age_ms=500,
        max_signal_age_ms=500,
        max_clock_uncertainty_ms=5.0,
        minimum_required_confidence=0.9,
        required_signal_types=["protective-zone-clear"],
        approval={
            "state": "approved",
            "approver_id": "safety-owner",
            "approved_at": NOW - timedelta(minutes=1),
        },
    )
    policy_record = store.register_model(policy, occurred_at=NOW)
    assert policy_record["approval_state"] == "approved"
    manifest_ref = next(
        item.manifest_ref for item in binding.assets if item.asset_id == "arm-01"
    )
    return store, SafetyV2Catalog(database, store), manifest_ref, policy_record


def _safety_ingress(
    manifest_ref,
    policy_record,
    *,
    snapshot_id: str = "snapshot-01",
    sequence: int = 1,
    confidence: float | None = 1.0,
    captured_at: datetime = NOW,
    calibration_refs: list[dict] | None = None,
) -> SafetyEvidenceIngressV2:
    signal = {
        "signal_id": f"zone-clear-{snapshot_id}",
        "signal_type": "protective-zone-clear",
        "sampled_at": captured_at,
        "asset_ids": ["arm-01"],
        "value": True,
        "required": True,
        "valid": True,
    }
    if confidence is not None:
        signal["confidence"] = confidence
    return SafetyEvidenceIngressV2.model_validate(
        {
            "snapshot_id": snapshot_id,
            "producer_id": "unity-observer",
            "producer_boot_id": UUID("11111111-1111-4111-8111-111111111111"),
            "sequence": sequence,
            "run_id": "safety-run",
            "captured_at": captured_at,
            "asset_ids": ["arm-01"],
            "manifest_refs": [
                {"asset_id": "arm-01", "manifest_ref": manifest_ref}
            ],
            "calibration_refs": calibration_refs or [],
            "policy_ref": {
                "object_id": "cell-safety-policy",
                "version": 1,
                "sha256": policy_record["payload_sha256"],
            },
            "signals": [signal],
        }
    )


def _clock(*, state: str = "synchronized") -> ClockMeasurementV2:
    return ClockMeasurementV2(
        monitor_id="central-clock-monitor",
        reference_id="lab-ptp",
        sampled_at=NOW,
        offset_ms=0.2,
        uncertainty_ms=0.5,
        state=state,
    )


def test_safety_v2_safe_provenance_is_central_derived(tmp_path):
    store, catalog, manifest_ref, policy_record = _active_safety_fixture(tmp_path)
    ingress = _safety_ingress(manifest_ref, policy_record)
    result = catalog.accept(ingress, received_at=NOW, measured_clock=_clock())
    assert result["state"] == "SAFE"
    assert result["source_kind"] == "synthetic"
    assert result["execution_mode"] == "SIMULATION"
    assert result["physical_estop_claimed"] is False
    assert store.get_run("safety-run")["state"] == "ACTIVE"

    with pytest.raises(ValidationError):
        SafetyEvidenceIngressV2.model_validate(
            {**ingress.model_dump(mode="json"), "source_kind": "live"}
        )


@pytest.mark.parametrize(
    ("confidence", "clock_state", "expected_code"),
    [
        (None, "synchronized", "REQUIRED_CONFIDENCE_MISSING"),
        (1.0, "degraded", "CLOCK_NOT_SYNCHRONIZED"),
    ],
)
def test_safety_v2_missing_trust_fails_closed_and_holds(
    tmp_path, confidence, clock_state, expected_code
):
    store, catalog, manifest_ref, policy_record = _active_safety_fixture(tmp_path)
    result = catalog.accept(
        _safety_ingress(manifest_ref, policy_record, confidence=confidence),
        received_at=NOW,
        measured_clock=_clock(state=clock_state),
    )
    assert result["state"] == "UNKNOWN"
    assert result["run_state"] == "HELD"
    assert expected_code in {reason["code"] for reason in result["reasons"]}
    assert store.get_run("safety-run")["state"] == "HELD"


def test_safety_assessment_and_hold_rollback_together(tmp_path, monkeypatch):
    store, catalog, manifest_ref, policy_record = _active_safety_fixture(tmp_path)

    def fail_hold(*args, **kwargs):
        raise RuntimeError("injected hold failure")

    monkeypatch.setattr(store, "hold_run_in_transaction", fail_hold)
    with pytest.raises(RuntimeError, match="injected hold failure"):
        catalog.accept(
            _safety_ingress(manifest_ref, policy_record, confidence=None),
            received_at=NOW,
            measured_clock=_clock(),
        )

    assert catalog.list_assessments("safety-run") == []
    assert store.get_run("safety-run")["state"] == "ACTIVE"


def test_safety_v2_stale_or_sequence_regression_fails_closed(tmp_path):
    store, catalog, manifest_ref, policy_record = _active_safety_fixture(tmp_path)
    first = catalog.accept(
        _safety_ingress(
            manifest_ref,
            policy_record,
            snapshot_id="snapshot-first",
            sequence=7,
        ),
        received_at=NOW,
        measured_clock=_clock(),
    )
    assert first["state"] == "SAFE"

    regressed = catalog.accept(
        _safety_ingress(
            manifest_ref,
            policy_record,
            snapshot_id="snapshot-regressed",
            sequence=7,
        ),
        received_at=NOW,
        measured_clock=_clock(),
    )
    assert regressed["state"] == "UNKNOWN"
    assert regressed["run_state"] == "HELD"
    assert "SOURCE_ORDER_INVALID" in {
        reason["code"] for reason in regressed["reasons"]
    }


def test_safety_v2_required_calibration_is_not_defaulted(tmp_path):
    store, catalog, manifest_ref, policy_record = _active_safety_fixture(tmp_path)
    policy = SafetyPolicyV2.model_validate(
        {
            **policy_record["payload"],
            "version": 2,
            "required_calibration_refs": [
                {"object_id": "cell-world", "version": 1, "sha256": SHA_A}
            ],
        }
    )
    policy_record = store.register_model(policy, occurred_at=NOW)
    base_ingress = _safety_ingress(manifest_ref, policy_record)
    ingress = SafetyEvidenceIngressV2.model_validate(
        {
            **base_ingress.model_dump(mode="json"),
            "policy_ref": {
                "object_id": "cell-safety-policy",
                "version": 2,
                "sha256": policy_record["payload_sha256"],
            },
        }
    )
    result = catalog.accept(ingress, received_at=NOW, measured_clock=_clock())
    assert result["state"] == "UNKNOWN"
    assert "CALIBRATION_MISSING" in {
        reason["code"] for reason in result["reasons"]
    }


def _xr_update(session_id: UUID, principal_id: str, role: str, **changes):
    values = {
        "session_id": session_id,
        "principal_id": principal_id,
        "role": role,
        "run_id": "safety-run",
        "sequence": 1,
        "connected": True,
        "focused": True,
        "tracking": True,
        "deadman": True,
        "sampled_at": NOW,
    }
    values.update(changes)
    return XrSessionUpdateV1.model_validate(values)


def test_xr_loss_is_latched_and_cannot_self_recover(tmp_path):
    store, _, _, _ = _active_safety_fixture(tmp_path)
    authority = ControlAuthority(tmp_path / "fleet.db", store)
    authority.bind_role("edge-user", "edge_operator", now=NOW)
    session_id = UUID("22222222-2222-4222-8222-222222222222")
    healthy = authority.update_xr_session(
        _xr_update(session_id, "edge-user", "edge_operator"),
        received_at=NOW,
    )
    assert healthy["latched"] is False
    lost = authority.update_xr_session(
        _xr_update(
            session_id,
            "edge-user",
            "edge_operator",
            sequence=2,
            focused=False,
            sampled_at=NOW + timedelta(milliseconds=100),
        ),
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert lost["latched"] is True
    restored_bits = authority.update_xr_session(
        _xr_update(
            session_id,
            "edge-user",
            "edge_operator",
            sequence=3,
            sampled_at=NOW + timedelta(milliseconds=200),
        ),
        received_at=NOW + timedelta(milliseconds=200),
    )
    assert restored_bits["latched"] is True
    with pytest.raises(ControlAuthorityError, match="latched"):
        authority.require_active_xr_session(
            session_id,
            principal_id="edge-user",
            run_id="safety-run",
            now=NOW + timedelta(milliseconds=200),
        )


def test_xr_old_or_regressed_samples_cannot_refresh_intervention(tmp_path):
    store, _, _, _ = _active_safety_fixture(tmp_path)
    authority = ControlAuthority(tmp_path / "fleet.db", store)
    authority.bind_role("edge-user", "edge_operator", now=NOW)
    old_session = UUID("77777777-7777-4777-8777-777777777777")
    old = authority.update_xr_session(
        _xr_update(
            old_session,
            "edge-user",
            "edge_operator",
            sampled_at=NOW - timedelta(seconds=2),
        ),
        received_at=NOW,
    )
    assert old["latched"] is True
    assert old["latch_reason"] == "lost:sample_age"

    live_session = UUID("88888888-8888-4888-8888-888888888888")
    authority.update_xr_session(
        _xr_update(live_session, "edge-user", "edge_operator"),
        received_at=NOW,
    )
    with pytest.raises(ControlAuthorityError, match="sample time regression"):
        authority.update_xr_session(
            _xr_update(
                live_session,
                "edge-user",
                "edge_operator",
                sequence=2,
                sampled_at=NOW,
            ),
            received_at=NOW + timedelta(milliseconds=100),
        )


def test_held_run_requires_independent_edge_and_manager_approvals(tmp_path):
    store, _, _, _ = _active_safety_fixture(tmp_path)
    store.hold_run("safety-run", reason="fixture-unsafe", occurred_at=NOW)
    authority = ControlAuthority(tmp_path / "fleet.db", store)
    authority.bind_role("edge-user", "edge_operator", now=NOW)
    authority.bind_role("manager-user", "manager", now=NOW)
    edge_session = UUID("33333333-3333-4333-8333-333333333333")
    manager_session = UUID("44444444-4444-4444-8444-444444444444")
    authority.update_xr_session(
        _xr_update(edge_session, "edge-user", "edge_operator"),
        received_at=NOW,
    )
    authority.update_xr_session(
        _xr_update(manager_session, "manager-user", "manager"),
        received_at=NOW,
    )
    challenge = authority.begin_resume(
        "safety-run", now=NOW, ttl=timedelta(minutes=2)
    )
    first = authority.approve_resume(
        challenge["challenge_id"],
        principal_id="edge-user",
        session_id=edge_session,
        now=NOW,
    )
    assert first["state"] == "PENDING"
    assert store.get_run("safety-run")["state"] == "HELD"
    second = authority.approve_resume(
        challenge["challenge_id"],
        principal_id="manager-user",
        session_id=manager_session,
        now=NOW + timedelta(milliseconds=100),
    )
    assert second["state"] == "APPROVED"
    assert second["run_state"] == "ACTIVE"
    assert second["physical_estop_claimed"] is False


def test_second_resume_approval_rolls_back_if_run_transition_fails(
    tmp_path, monkeypatch
):
    database = tmp_path / "fleet.db"
    store, _, _, _ = _active_safety_fixture(tmp_path)
    store.hold_run("safety-run", reason="fixture-unsafe", occurred_at=NOW)
    authority = ControlAuthority(database, store)
    authority.bind_role("edge-user", "edge_operator", now=NOW)
    authority.bind_role("manager-user", "manager", now=NOW)
    edge_session = UUID("55555555-5555-4555-8555-555555555555")
    manager_session = UUID("66666666-6666-4666-8666-666666666666")
    authority.update_xr_session(
        _xr_update(edge_session, "edge-user", "edge_operator"), received_at=NOW
    )
    authority.update_xr_session(
        _xr_update(manager_session, "manager-user", "manager"), received_at=NOW
    )
    challenge = authority.begin_resume(
        "safety-run", now=NOW, ttl=timedelta(minutes=2)
    )
    authority.approve_resume(
        challenge["challenge_id"],
        principal_id="edge-user",
        session_id=edge_session,
        now=NOW,
    )

    def fail_transition(*args, **kwargs):
        raise RuntimeError("injected transition failure")

    monkeypatch.setattr(store, "transition_run_in_transaction", fail_transition)
    with pytest.raises(RuntimeError, match="injected transition failure"):
        authority.approve_resume(
            challenge["challenge_id"],
            principal_id="manager-user",
            session_id=manager_session,
            now=NOW + timedelta(milliseconds=100),
        )

    with sqlite3.connect(database) as connection:
        approval_count = connection.execute(
            "SELECT COUNT(*) FROM resume_approvals WHERE challenge_id = ?",
            (challenge["challenge_id"],),
        ).fetchone()[0]
        challenge_state = connection.execute(
            "SELECT state FROM resume_challenges WHERE challenge_id = ?",
            (challenge["challenge_id"],),
        ).fetchone()[0]
    assert approval_count == 1
    assert challenge_state == "PENDING"
    assert store.get_run("safety-run")["state"] == "HELD"
