from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from fleet_control_plane.orchestration_contracts import (
    SCHEMA_MODELS,
    export_schemas,
    orchestration_contract_schemas,
)
from fleet_control_plane.orchestration_models import (
    CapabilityDefinitionV1,
    CapabilityProfileV1,
    ControlEnvelopeV1,
    ControlSubleaseV1,
    CoordinatedGroupV1,
    DelegationGrantV1,
    DeviceManifestV1,
    RuntimeBindingV1,
    SafetySnapshotV2,
    UnityBuildAttestationV1,
    WorkflowDefinitionV2,
    parse_role_cardinality,
    validate_control_sublease,
    validate_runtime_binding,
)


ROOT = Path(__file__).parents[3]
CONTRACT_DIR = ROOT / "contracts" / "orchestration"
DESIGN_PATH = ROOT / "docs" / "MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
GRANT_ID = UUID("10000000-0000-4000-8000-000000000001")
BOOT_ID = UUID("20000000-0000-4000-8000-000000000002")
LEASE_ID = UUID("30000000-0000-4000-8000-000000000003")
COMMAND_ID = UUID("40000000-0000-4000-8000-000000000004")


def _motion_capability(*, commissioned_timing: bool = True) -> dict[str, object]:
    timing = 5.0 if commissioned_timing else 0.0
    return {
        "capability_id": "execute-registered-joint-plan",
        "version": 1,
        "effect": "actuation",
        "parameter_schema_ref": "contracts://capabilities/joint-plan.v1",
        "required_frames": ["base", "tool"],
        "required_units": ["joint_position"],
        "required_limits": ["joint_position", "joint_velocity"],
        "control_envelope_schema_ref": "contracts://envelopes/joint-plan.v1",
        "completion_contract_ref": "contracts://completion/joint-converged.v1",
        "cancellation_contract_ref": "contracts://cancellation/joint-plan.v1",
        "recovery_contract_ref": "contracts://recovery/joint-plan.v1",
        "evidence_contract_ref": "contracts://evidence/joint-plan.v1",
        "allowed_execution_modes": ["SIMULATION", "HIL", "PHYSICAL_AUTO"],
        "max_clock_uncertainty_ms": timing,
        "max_demand_age_ms": timing,
        "on_lease_expiry": "safe_state",
        "on_agent_restart_with_motion_in_flight": "vendor_state_reconcile",
    }


def _camera_capability() -> dict[str, object]:
    return {
        "capability_id": "record-compressed-video",
        "version": 1,
        "effect": "observation",
        "parameter_schema_ref": "contracts://capabilities/compressed-video.v1",
        "allowed_execution_modes": [
            "OBSERVE_ONLY",
            "SIMULATION",
            "REPLAY",
            "SHADOW",
            "HIL",
            "PHYSICAL_AUTO",
        ],
    }


def _capability_models(*, commissioned_timing: bool = True):
    return [
        CapabilityDefinitionV1.model_validate(
            _motion_capability(commissioned_timing=commissioned_timing)
        ),
        CapabilityDefinitionV1.model_validate(_camera_capability()),
    ]


def _workflow_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "workflow_id": "generic-position-and-observe",
        "version": 1,
        "roles": [
            {
                "role": "handler",
                "cardinality": "1",
                "selector": {
                    "requires": ["execute-registered-joint-plan@1"]
                },
                "access": "exclusive",
            },
            {
                "role": "cell-view",
                "cardinality": "1..n",
                "selector": {
                    "requires": ["record-compressed-video@1"],
                    "zone_id": "process-cell",
                },
                "access": "shared_read",
            },
        ],
        "steps": [
            {
                "step_id": "position",
                "kind": "action",
                "role": "handler",
                "capability": "execute-registered-joint-plan@1",
                "parameters": {"plan_ref": "artifact://plans/position@1"},
                "timeout_s": 30.0,
            },
            {
                "step_id": "approval",
                "kind": "human_approval",
                "authority": "manager",
                "depends_on": ["position"],
                "timeout_s": 60.0,
            },
            {
                "step_id": "process",
                "kind": "action",
                "role": "handler",
                "capability": "execute-registered-joint-plan@1",
                "parameters": {"plan_ref": "artifact://plans/process@1"},
                "depends_on": ["approval"],
                "timeout_s": 60.0,
            },
            {
                "step_id": "capture",
                "kind": "action",
                "role": "cell-view",
                "capability": "record-compressed-video@1",
                "parameters": {"recording_profile_ref": "artifact://video/mjpg@1"},
                "depends_on": ["approval"],
                "timeout_s": 60.0,
                "fan_out": "all_bound_assets",
                "join": "all",
                "lifetime": {
                    "start_on": "approval:SUCCEEDED",
                    "stop_on": "process:TERMINAL",
                    "on_unreachable": "cancel",
                },
            },
        ],
    }


def _registry_ref(object_id: str) -> dict[str, object]:
    return {"object_id": object_id, "version": 1, "sha256": SHA_B}


def _runtime_asset(
    asset_id: str,
    capabilities: list[str],
    *,
    runtime_class: str = "simulation",
    execution_mode: str = "SIMULATION",
    command_sink_present: bool = False,
    commissioned_capabilities: list[str] | None = None,
) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "runtime_class": runtime_class,
        "manifest_ref": {
            "manifest_id": f"{asset_id}-manifest",
            "version": 1,
            "sha256": SHA_A,
        },
        "connection_ref": _registry_ref(f"{asset_id}-connection"),
        "calibration_refs": [],
        "capabilities": capabilities,
        "supported_modes": [execution_mode],
        "command_sink_present": command_sink_present,
        "commissioned_capabilities": commissioned_capabilities or [],
    }


def _binding_payload(
    camera_count: int,
    *,
    execution_mode: str = "SIMULATION",
    runtime_class: str = "simulation",
    command_sink_present: bool = False,
    commissioned_capabilities: list[str] | None = None,
) -> dict[str, object]:
    camera_ids = [f"camera-{index:02d}" for index in range(1, camera_count + 1)]
    assets = [
        _runtime_asset(
            "arm-01",
            ["execute-registered-joint-plan@1"],
            runtime_class=runtime_class,
            execution_mode=execution_mode,
            command_sink_present=command_sink_present,
            commissioned_capabilities=commissioned_capabilities,
        )
    ]
    assets.extend(
        _runtime_asset(
            camera_id,
            ["record-compressed-video@1"],
            runtime_class=runtime_class,
            execution_mode=execution_mode,
        )
        for camera_id in camera_ids
    )
    return {
        "schema_version": 1,
        "binding_id": f"binding-{camera_count}-cameras",
        "run_id": f"run-{camera_count}-cameras",
        "workflow_ref": {
            "workflow_id": "generic-position-and-observe",
            "version": 1,
        },
        "execution_mode": execution_mode,
        "assets": assets,
        "role_bindings": [
            {"role": "handler", "asset_ids": ["arm-01"]},
            {"role": "cell-view", "asset_ids": camera_ids},
        ],
    }


def _grant_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "grant_id": str(GRANT_ID),
        "run_id": "run-physical-test",
        "orchestrator_id": "processing-lab1/orchestrator",
        "orchestrator_boot_id": str(BOOT_ID),
        "asset_id": "arm-01",
        "execution_mode": "PHYSICAL_AUTO",
        "authorization_id": "authorization-01",
        "manifest_sha256": SHA_A,
        "control_envelope_id": "slow-joint-envelope",
        "control_envelope_sha256": SHA_B,
        "epoch": 7,
        "capabilities": ["execute-registered-joint-plan@1"],
        "delegated_key_id": "orchestrator-key-01",
        "delegated_public_key_ref": "secrets://public/orchestrator-key-01",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
        "renewable_until": (NOW + timedelta(seconds=120)).isoformat(),
        "max_sublease_ttl_ms": 10_000,
        "issuer_key_id": "central-signing-key-01",
        "signature": "central-signature-placeholder",
    }


def _sublease_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "lease_id": str(LEASE_ID),
        "lease_kind": "auto",
        "parent_grant_id": str(GRANT_ID),
        "run_id": "run-physical-test",
        "orchestrator_id": "processing-lab1/orchestrator",
        "orchestrator_boot_id": str(BOOT_ID),
        "command_id": str(COMMAND_ID),
        "subject": {"kind": "service", "principal_id": "cell-orchestrator"},
        "asset_id": "arm-01",
        "capabilities": ["execute-registered-joint-plan@1"],
        "execution_mode": "PHYSICAL_AUTO",
        "authorization_id": "authorization-01",
        "control_envelope_id": "slow-joint-envelope",
        "control_envelope_sha256": SHA_B,
        "epoch": 7,
        "issued_at": (NOW + timedelta(seconds=1)).isoformat(),
        "expires_at": (NOW + timedelta(seconds=9)).isoformat(),
        "renewable_until": (NOW + timedelta(seconds=120)).isoformat(),
        "issuer_key_id": "orchestrator-key-01",
        "signature": "delegated-signature-placeholder",
    }


def _control_envelope() -> ControlEnvelopeV1:
    return ControlEnvelopeV1.model_validate(
        {
            "schema_version": 1,
            "envelope_id": "slow-joint-envelope",
            "version": 1,
            "capabilities": ["execute-registered-joint-plan@1"],
            "max_rate_hz": 20.0,
            "max_demand_age_ms": 50.0,
            "max_clock_uncertainty_ms": 5.0,
            "declared_time_to_safe_state_ms": 500.0,
            "max_delta_by_axis": {"joint-1": 0.01},
            "approval": {
                "state": "approved",
                "approver_id": "safety-officer-01",
                "approved_at": (NOW - timedelta(seconds=1)).isoformat(),
                "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
            },
        }
    )


def _design_json_block(section_heading: str) -> dict[str, object]:
    document = DESIGN_PATH.read_text(encoding="utf-8")
    section_start = document.index(section_heading)
    block_start = document.index("```json\n", section_start) + len("```json\n")
    block_end = document.index("\n```", block_start)
    return json.loads(document[block_start:block_end])


def _safety_snapshot_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "snapshot_id": "snapshot-01",
        "producer_id": "unity-observer-01",
        "producer_boot_id": "50000000-0000-4000-8000-000000000005",
        "sequence": 42,
        "run_id": "run-simulation-01",
        "captured_at": NOW.isoformat(),
        "received_at": (NOW + timedelta(milliseconds=10)).isoformat(),
        "clock": {
            "monitor_id": "chrony-processing-lab1",
            "reference_id": "ptp-grandmaster-01",
            "sampled_at": NOW.isoformat(),
            "offset_ms": 0.2,
            "uncertainty_ms": 0.5,
            "state": "synchronized",
        },
        "asset_ids": ["arm-01"],
        "manifest_refs": [
            {
                "asset_id": "arm-01",
                "manifest_ref": {
                    "manifest_id": "arm-manifest",
                    "version": 1,
                    "sha256": SHA_A,
                },
            }
        ],
        "zone_refs": [_registry_ref("process-cell")],
        "calibration_refs": [_registry_ref("world-to-arm")],
        "policy_ref": _registry_ref("scenario-safety-policy"),
        "separations": [
            {
                "observation_id": "distance-01",
                "human_id": "human-01",
                "asset_id": "arm-01",
                "sampled_at": NOW.isoformat(),
                "frame_id": "world",
                "calibration_ref": _registry_ref("world-to-arm"),
                "distance_m": 1.2,
                "valid": True,
                "confidence": None,
            }
        ],
        "signals": [
            {
                "signal_id": "guard-status",
                "signal_type": "guard_closed",
                "sampled_at": NOW.isoformat(),
                "asset_ids": ["arm-01"],
                "value": True,
                "required": True,
                "valid": True,
                "confidence": None,
            }
        ],
        "command_emitted": False,
    }


def test_capability_profile_requires_complete_effect_contract():
    profile = CapabilityProfileV1.model_validate(
        {
            "schema_version": 1,
            "profile_id": "generic-arm",
            "version": 1,
            "kind_tags": ["actuated", "manipulator"],
            "capabilities": [_motion_capability(), _camera_capability()],
        }
    )
    assert [item.ref for item in profile.capabilities] == [
        "execute-registered-joint-plan@1",
        "record-compressed-video@1",
    ]

    incomplete = _motion_capability()
    incomplete.pop("cancellation_contract_ref")
    with pytest.raises(ValidationError, match="complete execution contracts"):
        CapabilityDefinitionV1.model_validate(incomplete)


@pytest.mark.parametrize(
    ("wire_value", "expected"),
    [("1", (1, 1)), ("1..n", (1, None)), ("2..12", (2, 12))],
)
def test_role_cardinality_parser(wire_value, expected):
    assert parse_role_cardinality(wire_value) == expected


def test_same_workflow_binds_three_and_twelve_cameras_without_edits():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())
    original = workflow.model_dump_json()

    for camera_count in (3, 12):
        binding = RuntimeBindingV1.model_validate(_binding_payload(camera_count))
        validate_runtime_binding(workflow, binding, _capability_models())
        assert len(binding.role_bindings[1].asset_ids) == camera_count

    assert workflow.model_dump_json() == original


def test_committed_camera_scaling_examples_are_valid_and_command_free():
    examples = CONTRACT_DIR / "examples"
    profile = CapabilityProfileV1.model_validate_json(
        (examples / "articulated-arm-profile.v1.json").read_text(encoding="utf-8")
    )
    manifest = DeviceManifestV1.model_validate_json(
        (examples / "example-arm-manifest.v1.json").read_text(encoding="utf-8")
    )
    assert manifest.profile_ref.profile_id == profile.profile_id
    assert set(manifest.capability_refs) == {
        capability.ref for capability in profile.capabilities
    }
    assert manifest.execution_contract.command_sink_present is False

    workflow = WorkflowDefinitionV2.model_validate_json(
        (examples / "generic-position-and-observe.v2.json").read_text(
            encoding="utf-8"
        )
    )
    workflow_dump = workflow.model_dump_json()

    for camera_count in (3, 12):
        binding = RuntimeBindingV1.model_validate_json(
            (examples / f"simulation-binding-{camera_count}-cameras.v1.json").read_text(
                encoding="utf-8"
            )
        )
        validate_runtime_binding(workflow, binding, _capability_models())
        assert binding.execution_mode == "SIMULATION"
        assert all(asset.runtime_class == "simulation" for asset in binding.assets)
        assert all(not asset.command_sink_present for asset in binding.assets)
        assert len(binding.role_bindings[1].asset_ids) == camera_count

    assert workflow.model_dump_json() == workflow_dump


def test_normative_design_json_examples_match_frozen_contracts():
    CapabilityProfileV1.model_validate(
        _design_json_block("### 5.2 Capability profile")
    )
    DeviceManifestV1.model_validate(_design_json_block("### 5.3 Device manifest"))
    WorkflowDefinitionV2.model_validate(_design_json_block("### 7.1 Workflow schema"))


def test_binding_rejects_capability_cardinality_and_runtime_mismatches():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())

    no_cameras = _binding_payload(1)
    no_cameras["role_bindings"][1]["asset_ids"] = []
    with pytest.raises(ValidationError, match="at least 1 item"):
        RuntimeBindingV1.model_validate(no_cameras)

    missing_capability = _binding_payload(3)
    missing_capability["assets"][1]["capabilities"] = ["other-camera@1"]
    with pytest.raises(ValueError, match="lacks role cell-view capabilities"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(missing_capability),
            _capability_models(),
        )

    physical_in_simulation = _binding_payload(3)
    physical_in_simulation["assets"][0]["runtime_class"] = "physical"
    with pytest.raises(ValueError, match="incompatible with SIMULATION"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(physical_in_simulation),
            _capability_models(),
        )


def test_physical_binding_requires_sink_commissioning_and_nonzero_timing():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())
    payload = _binding_payload(
        3,
        execution_mode="PHYSICAL_AUTO",
        runtime_class="physical",
        command_sink_present=True,
        commissioned_capabilities=["execute-registered-joint-plan@1"],
    )
    binding = RuntimeBindingV1.model_validate(payload)

    with pytest.raises(ValueError, match="not commissioned"):
        validate_runtime_binding(
            workflow,
            binding,
            _capability_models(commissioned_timing=False),
        )

    validate_runtime_binding(workflow, binding, _capability_models())

    payload["assets"][0]["commissioned_capabilities"] = []
    uncommissioned = RuntimeBindingV1.model_validate(payload)
    with pytest.raises(ValueError, match="is not commissioned"):
        validate_runtime_binding(workflow, uncommissioned, _capability_models())

    payload["assets"][0]["command_sink_present"] = False
    no_sink = RuntimeBindingV1.model_validate(payload)
    with pytest.raises(ValueError, match="has no command sink"):
        validate_runtime_binding(workflow, no_sink, _capability_models())


def test_hil_physical_effect_requires_pinned_output_isolation_evidence():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())
    payload = _binding_payload(
        3,
        execution_mode="HIL",
        runtime_class="physical",
        command_sink_present=True,
    )
    binding = RuntimeBindingV1.model_validate(payload)
    with pytest.raises(ValueError, match="output-isolation evidence"):
        validate_runtime_binding(workflow, binding, _capability_models())

    payload["assets"][0]["output_isolation_evidence_ref"] = _registry_ref(
        "hil-output-isolation"
    )
    isolated = RuntimeBindingV1.model_validate(payload)
    validate_runtime_binding(
        workflow,
        isolated,
        _capability_models(commissioned_timing=False),
    )


def test_effect_free_mode_rejects_actuation_even_if_capability_claims_support():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())
    payload = _binding_payload(
        3,
        execution_mode="SHADOW",
        runtime_class="physical",
    )
    definitions = _capability_models()
    motion_payload = _motion_capability()
    motion_payload["allowed_execution_modes"].append("SHADOW")
    definitions[0] = CapabilityDefinitionV1.model_validate(motion_payload)
    with pytest.raises(ValueError, match="forbids effectful capability"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(payload),
            definitions,
        )


def test_binding_rejects_exclusive_reuse_version_role_and_definition_gaps():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())

    reused = _binding_payload(1)
    reused["assets"][0]["capabilities"].append("record-compressed-video@1")
    reused["assets"] = [reused["assets"][0]]
    reused["role_bindings"][1]["asset_ids"] = ["arm-01"]
    with pytest.raises(ValueError, match="exclusive asset"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(reused),
            _capability_models(),
        )

    wrong_version = _binding_payload(1)
    wrong_version["workflow_ref"]["version"] = 2
    with pytest.raises(ValueError, match="different workflow version"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(wrong_version),
            _capability_models(),
        )

    missing_role = _binding_payload(1)
    missing_role["role_bindings"] = missing_role["role_bindings"][:1]
    with pytest.raises(ValueError, match="role bindings mismatch"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(missing_role),
            _capability_models(),
        )

    with pytest.raises(ValueError, match="missing capability definition"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(_binding_payload(1)),
            _capability_models()[1:],
        )

    duplicate_definitions = _capability_models()
    duplicate_definitions.append(duplicate_definitions[0])
    with pytest.raises(ValueError, match="capability definitions must be unique"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(_binding_payload(1)),
            duplicate_definitions,
        )


def test_coordinated_group_binding_transitively_excludes_member_assets():
    workflow = WorkflowDefinitionV2.model_validate(_workflow_payload())
    payload = _binding_payload(3)
    payload["assets"][0]["coordinated_group_ref"] = _registry_ref("arm-01")
    binding = RuntimeBindingV1.model_validate(payload)
    group_payload = {
        "schema_version": 1,
        "group_asset_id": "arm-01",
        "version": 1,
        "member_asset_ids": ["member-arm-a", "member-arm-b"],
        "capabilities": ["execute-registered-joint-plan@1"],
        "max_command_skew_ms": 5.0,
    }
    group = CoordinatedGroupV1.model_validate(group_payload)
    validate_runtime_binding(workflow, binding, _capability_models(), [group])

    group_payload["member_asset_ids"] = ["camera-01", "member-arm-b"]
    overlapping = CoordinatedGroupV1.model_validate(group_payload)
    with pytest.raises(ValueError, match="cannot be bound independently"):
        validate_runtime_binding(
            workflow,
            binding,
            _capability_models(),
            [overlapping],
        )


def test_workflow_rejects_cycles_missing_capability_and_connection_details():
    cycle = _workflow_payload()
    cycle["steps"][0]["depends_on"] = ["process"]
    with pytest.raises(ValidationError, match="contains a cycle"):
        WorkflowDefinitionV2.model_validate(cycle)

    wrong_capability = _workflow_payload()
    wrong_capability["steps"][0]["capability"] = "other-motion@1"
    with pytest.raises(ValidationError, match="absent from role selector"):
        WorkflowDefinitionV2.model_validate(wrong_capability)

    deployment_specific = _workflow_payload()
    deployment_specific["steps"][0]["parameters"]["ip_address"] = "10.0.0.7"
    with pytest.raises(ValidationError, match="deployment-specific field"):
        WorkflowDefinitionV2.model_validate(deployment_specific)

    deployment_values = _workflow_payload()
    deployment_values["steps"][0]["parameters"] = {
        "endpoint": "tcp://10.0.0.7:5000",
        "host": "192.168.0.5",
        "model": "RB5-850",
        "namespace": "/rb5/arm",
        "scene": "Scenario1_app",
    }
    with pytest.raises(ValidationError, match="deployment-specific field"):
        WorkflowDefinitionV2.model_validate(deployment_values)

    predicate_value = _workflow_payload()
    predicate_value["steps"].append(
        {
            "step_id": "unsafe-route",
            "kind": "conditional",
            "cases": [
                {
                    "when": {
                        "path": "steps.process.result.endpoint",
                        "op": "eq",
                        "value": "tcp://10.0.0.7:5000",
                    },
                    "next": "process",
                },
                {"when": {"op": "default"}, "next": "capture"},
            ],
        }
    )
    with pytest.raises(ValidationError, match="network address|deployment URI"):
        WorkflowDefinitionV2.model_validate(predicate_value)

    human_instruction = _workflow_payload()
    human_instruction["steps"].append(
        {
            "step_id": "unsafe-human-task",
            "kind": "human_task",
            "authority": "edge-operator",
            "instruction": "Inspect /rb5/arm before continuing.",
            "timeout_s": 30,
        }
    )
    with pytest.raises(ValidationError, match="absolute topic or path"):
        WorkflowDefinitionV2.model_validate(human_instruction)


def test_workflow_requires_structured_predicates_and_multi_asset_fanout():
    raw_predicate = _workflow_payload()
    raw_predicate["steps"].append(
        {
            "step_id": "wait",
            "kind": "wait_for_observation",
            "role": "cell-view",
            "capability": "record-compressed-video@1",
            "predicate": "result.ok == true",
            "timeout_s": 10,
            "fan_out": "all_bound_assets",
            "join": "all",
        }
    )
    with pytest.raises(ValidationError):
        WorkflowDefinitionV2.model_validate(raw_predicate)

    no_fanout = _workflow_payload()
    no_fanout["steps"][3].pop("fan_out")
    no_fanout["steps"][3].pop("join")
    with pytest.raises(ValidationError, match="requires fan_out and join"):
        WorkflowDefinitionV2.model_validate(no_fanout)

    compensation = _workflow_payload()
    compensation["steps"].append(
        {
            "step_id": "camera-compensation",
            "kind": "compensation",
            "role": "cell-view",
            "capability": "record-compressed-video@1",
            "timeout_s": 10,
        }
    )
    with pytest.raises(ValidationError, match="requires fan_out and join"):
        WorkflowDefinitionV2.model_validate(compensation)


def test_fanout_selection_is_bounded_by_role_and_actual_binding():
    too_many = _workflow_payload()
    too_many["steps"][3]["fan_out"] = "first_n"
    too_many["steps"][3]["fan_out_count"] = 4
    workflow = WorkflowDefinitionV2.model_validate(too_many)
    with pytest.raises(ValueError, match="fan_out_count exceeds"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(_binding_payload(3)),
            _capability_models(),
        )

    invalid_ordinal = _workflow_payload()
    invalid_ordinal["steps"][3]["fan_out"] = "explicit_binding_set"
    invalid_ordinal["steps"][3]["binding_ordinals"] = [0, 3]
    workflow = WorkflowDefinitionV2.model_validate(invalid_ordinal)
    with pytest.raises(ValueError, match="binding_ordinals exceed"):
        validate_runtime_binding(
            workflow,
            RuntimeBindingV1.model_validate(_binding_payload(3)),
            _capability_models(),
        )

    asset_specific = _workflow_payload()
    asset_specific["steps"][3]["parameters"]["asset_ids"] = ["camera-01"]
    with pytest.raises(ValidationError, match="deployment-specific field"):
        WorkflowDefinitionV2.model_validate(asset_specific)


def test_conditional_uses_documented_closed_predicate_wire_format():
    payload = _workflow_payload()
    payload["steps"].extend(
        [
            {
                "step_id": "route",
                "kind": "conditional",
                "depends_on": ["process", "capture"],
                "cases": [
                    {
                        "when": {
                            "path": "steps.process.result.quality",
                            "op": "eq",
                            "value": "PASS",
                        },
                        "next": "release",
                    },
                    {"when": {"op": "default"}, "next": "inspect"},
                ],
            },
            {
                "step_id": "release",
                "kind": "human_task",
                "authority": "edge_operator",
                "instruction": "Release the completed fixture.",
                "timeout_s": 60,
            },
            {
                "step_id": "inspect",
                "kind": "human_task",
                "authority": "edge_operator",
                "instruction": "Inspect the fixture and record the result.",
                "timeout_s": 60,
            },
        ]
    )
    workflow = WorkflowDefinitionV2.model_validate(payload)
    dumped = workflow.model_dump(mode="json", by_alias=True)
    route = next(step for step in dumped["steps"] if step["step_id"] == "route")
    assert route["cases"][0]["next"] == "release"
    assert route["cases"][0]["when"]["op"] == "eq"
    assert "kind" not in route["cases"][0]["when"]


def test_simulation_grant_is_available_for_g3_without_physical_authority():
    payload = _grant_payload()
    payload["execution_mode"] = "SIMULATION"
    grant = DelegationGrantV1.model_validate(payload)
    assert grant.execution_mode == "SIMULATION"


def test_hil_grant_and_sublease_require_the_same_output_isolation_evidence():
    grant_payload = _grant_payload()
    grant_payload["execution_mode"] = "HIL"
    with pytest.raises(ValidationError, match="output-isolation evidence"):
        DelegationGrantV1.model_validate(grant_payload)

    isolation_ref = _registry_ref("hil-output-isolation")
    grant_payload["output_isolation_evidence_ref"] = isolation_ref
    grant = DelegationGrantV1.model_validate(grant_payload)
    lease_payload = _sublease_payload()
    lease_payload["execution_mode"] = "HIL"
    lease_payload["output_isolation_evidence_ref"] = isolation_ref
    lease = ControlSubleaseV1.model_validate(lease_payload)
    validate_control_sublease(grant, lease, _control_envelope(), SHA_B)


def test_valid_sublease_is_fenced_by_every_parent_grant_field():
    grant = DelegationGrantV1.model_validate(_grant_payload())
    lease = ControlSubleaseV1.model_validate(_sublease_payload())
    validate_control_sublease(grant, lease, _control_envelope(), SHA_B)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("orchestrator_boot_id", UUID(int=99), "orchestrator_boot_id"),
        ("epoch", 8, "epoch"),
        ("control_envelope_sha256", SHA_C, "control_envelope_sha256"),
        ("capabilities", ["unapproved-motion@1"], "capabilities exceed"),
    ],
)
def test_sublease_rejects_parent_fence_mismatch(field, value, message):
    grant = DelegationGrantV1.model_validate(_grant_payload())
    lease = ControlSubleaseV1.model_validate(_sublease_payload())
    mismatched = lease.model_copy(update={field: value})
    with pytest.raises(ValueError, match=message):
        validate_control_sublease(grant, mismatched, _control_envelope(), SHA_B)


def test_sublease_rejects_excess_ttl_and_teleop_without_human_presence():
    grant = DelegationGrantV1.model_validate(_grant_payload())
    lease = ControlSubleaseV1.model_validate(_sublease_payload())
    too_long = lease.model_copy(
        update={"expires_at": lease.issued_at + timedelta(seconds=11)}
    )
    with pytest.raises(ValueError, match="TTL exceeds"):
        validate_control_sublease(grant, too_long, _control_envelope(), SHA_B)

    too_short = lease.model_copy(
        update={"expires_at": lease.issued_at + timedelta(milliseconds=100)}
    )
    with pytest.raises(ValueError, match="shorter than declared"):
        validate_control_sublease(grant, too_short, _control_envelope(), SHA_B)

    teleop = _sublease_payload()
    teleop["lease_kind"] = "teleop"
    teleop["execution_mode"] = "PHYSICAL_TELEOP"
    with pytest.raises(ValidationError, match="human subject"):
        ControlSubleaseV1.model_validate(teleop)


def test_sublease_rejects_unresolved_unapproved_or_expired_envelope():
    grant = DelegationGrantV1.model_validate(_grant_payload())
    lease = ControlSubleaseV1.model_validate(_sublease_payload())
    envelope = _control_envelope()

    with pytest.raises(ValueError, match="does not match parent grant"):
        validate_control_sublease(grant, lease, envelope, SHA_C)

    missing_capability = envelope.model_copy(
        update={"capabilities": ["record-compressed-video@1"]}
    )
    with pytest.raises(ValueError, match="exceed control envelope"):
        validate_control_sublease(grant, lease, missing_capability, SHA_B)

    draft_approval = envelope.approval.model_copy(
        update={"state": "draft", "approver_id": None, "approved_at": None}
    )
    draft = envelope.model_copy(update={"approval": draft_approval})
    with pytest.raises(ValueError, match="not approved"):
        validate_control_sublease(grant, lease, draft, SHA_B)

    expired_approval = envelope.approval.model_copy(
        update={"expires_at": lease.issued_at + timedelta(milliseconds=100)}
    )
    expired = envelope.model_copy(update={"approval": expired_approval})
    with pytest.raises(ValueError, match="approval expiry"):
        validate_control_sublease(grant, lease, expired, SHA_B)


def test_safety_snapshot_v2_has_derived_provenance_and_remains_command_free():
    snapshot = SafetySnapshotV2.model_validate(_safety_snapshot_payload())
    assert snapshot.signals[0].confidence is None
    assert snapshot.command_emitted is False

    producer_claim = _safety_snapshot_payload()
    producer_claim["source_kind"] = "live"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SafetySnapshotV2.model_validate(producer_claim)

    command_claim = _safety_snapshot_payload()
    command_claim["command_emitted"] = True
    with pytest.raises(ValidationError):
        SafetySnapshotV2.model_validate(command_claim)


def test_safety_workflow_state_rejects_executor_self_report():
    payload = _safety_snapshot_payload()
    payload["workflow_state"] = {
        "expected_step": "process",
        "expected_by_orchestrator_id": "processing-lab1/orchestrator",
        "observed_step": "process",
        "observed_by_producer_id": "independent-observer-01",
        "executor_id": "arm-01-agent",
        "observed_at": NOW.isoformat(),
    }
    payload["producer_id"] = "independent-observer-01"
    SafetySnapshotV2.model_validate(payload)

    payload["producer_id"] = "arm-01-agent"
    payload["workflow_state"]["observed_by_producer_id"] = "arm-01-agent"
    with pytest.raises(ValidationError, match="independent observer"):
        SafetySnapshotV2.model_validate(payload)


def test_safety_validity_requires_measurement_but_allows_missing_confidence():
    payload = _safety_snapshot_payload()
    payload["separations"][0]["distance_m"] = None
    with pytest.raises(ValidationError, match="require distance_m"):
        SafetySnapshotV2.model_validate(payload)

    occupancy = _safety_snapshot_payload()
    occupancy["zone_occupancies"] = [
        {
            "observation_id": "occupancy-01",
            "entity_id": "human-01",
            "asset_ids": ["arm-01"],
            "sampled_at": NOW.isoformat(),
            "zone_ref": _registry_ref("process-cell"),
            "occupied": None,
            "valid": True,
            "confidence": None,
        }
    ]
    with pytest.raises(ValidationError, match="require occupied"):
        SafetySnapshotV2.model_validate(occupancy)

    null_signal = _safety_snapshot_payload()
    null_signal["signals"][0]["value"] = None
    with pytest.raises(ValidationError, match="non-null value"):
        SafetySnapshotV2.model_validate(null_signal)


def test_unity_attestation_rejects_duplicate_assemblies():
    payload = {
        "schema_version": 1,
        "build_id": "scenario-observer-01",
        "build_role": "observer",
        "source_repository": "git+https://example.invalid/unity/scenario.git",
        "source_commit": "1234567890abcdef",
        "unity_version": "6000.5.6f1",
        "scene_profile_sha256": SHA_A,
        "assemblies": [
            {"assembly_name": "Sang.Observer", "sha256": SHA_B},
            {"assembly_name": "Sang.Observer", "sha256": SHA_C},
        ],
        "built_at": NOW.isoformat(),
        "signer_key_id": "unity-release-key-01",
        "signature": "unity-signature-placeholder",
    }
    with pytest.raises(ValidationError, match="assembly names"):
        UnityBuildAttestationV1.model_validate(payload)


def test_unknown_fields_are_forbidden():
    profile = {
        "schema_version": 1,
        "profile_id": "camera",
        "version": 1,
        "capabilities": [_camera_capability()],
        "vendor_endpoint": "10.0.0.7",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CapabilityProfileV1.model_validate(profile)


def test_generated_schemas_match_committed_files_and_have_draft_ids():
    schemas = orchestration_contract_schemas()
    assert set(schemas) == set(SCHEMA_MODELS)
    for filename, schema in schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(f"/contracts/orchestration/{filename}")
        committed = json.loads((CONTRACT_DIR / filename).read_text(encoding="utf-8"))
        assert committed == schema
    assert export_schemas(CONTRACT_DIR, check=True) == []


def test_g0_contract_code_contains_no_runtime_command_transport():
    package = ROOT / "src" / "fleet_control_plane" / "fleet_control_plane"
    forbidden_roots = {
        "grpc",
        "http",
        "httpx",
        "paho",
        "rclpy",
        "requests",
        "socket",
        "urllib",
    }
    imported_roots: set[str] = set()
    for filename in ("orchestration_models.py", "orchestration_contracts.py"):
        tree = ast.parse((package / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(forbidden_roots)
