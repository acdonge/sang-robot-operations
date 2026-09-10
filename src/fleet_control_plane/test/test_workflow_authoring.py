from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fleet_control_plane.action_library import (
    ActionDraftCreateV1,
    ActionDraftReplaceV1,
    ActionLibraryInvalid,
    ActionPublishV1,
    ActionSimulationValidateV1,
)
from fleet_control_plane.app import create_app
from fleet_control_plane.operations_models import TaskDefinitionCreate
from fleet_control_plane.orchestration_store import OrchestrationStore
from fleet_control_plane.skill_catalog import (
    SkillCatalogV1,
    load_skill_catalog,
    validate_skill_parameters,
)
from fleet_control_plane.store import ConfigurationConflict, FleetStore
from fleet_control_plane.workflow_authoring import (
    CommissioningCandidateCreateV1,
    SimulationValidationProfileCreateV1,
    WorkflowAuthoringConflict,
    WorkflowAuthoringInvalid,
    WorkflowAuthoringStore,
    WorkflowAuthoringUnavailable,
    WorkflowDraftCloneV1,
    WorkflowDraftCreateV1,
    WorkflowDraftReplaceV1,
    WorkflowPublishV1,
    WorkflowSimulationControlV1,
    WorkflowSimulationQueueV1,
    load_unity_adapter_profile,
)
from fleet_control_plane.workflow_simulation_worker import _bridge_root
from fleet_control_plane.workflow_simulation_worker import UnityWorkflowWorker
import fleet_control_plane.workflow_simulation_worker as workflow_worker_module


WORKSPACE = Path(__file__).resolve().parents[3]
ADAPTER_PROFILE = WORKSPACE / "contracts/tasks/iitp-scenario1-unity-adapter.v1.json"
SKILL_CATALOG = WORKSPACE / "contracts/tasks/iitp-scenario1-skill-catalog.v2.json"
SCENARIO_WORKFLOW = WORKSPACE / "contracts/tasks/iitp-scenario1-orchestrated.v2.json"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

OMY_PLACE_ACTION_SOURCE = """public sealed class OmyPlaceOnWaamTableAction : ISangUnityRobotAction
{
    public string StepId => \"omy_pre_pick_place\";

    public bool TryBegin(
        Scenario1_noAnomaly scenario,
        float speedScale,
        out string detail)
    {
        return scenario.TryBeginOrchestratedStep(StepId, speedScale, out detail);
    }

    public string GetState(Scenario1_noAnomaly scenario, out string detail)
    {
        return scenario.GetOrchestratedStepState(StepId, out detail);
    }
}
"""


def stores(tmp_path):
    path = tmp_path / "fleet.db"
    fleet = FleetStore(path)
    OrchestrationStore(path)
    authoring = WorkflowAuthoringStore(
        path,
        adapter_profile_paths=[ADAPTER_PROFILE],
        skill_catalog_paths=[SKILL_CATALOG],
    )
    return fleet, authoring


def minimal_roles():
    return [
        {
            "role": "primary_robot",
            "asset_type": "robot",
            "access_mode": "exclusive",
            "cardinality": "1",
            "requires": ["move-home@1"],
        }
    ]


def minimal_steps():
    return [
        {
            "step_id": "move_home",
            "kind": "action",
            "depends_on": [],
            "role": "primary_robot",
            "capability": "move-home@1",
            "parameters": {"pose": "home"},
            "timeout_s": 30,
        }
    ]


def create_valid_draft(authoring, *, task_id="demo-task", workflow_id=None, prefix="a"):
    draft = authoring.create_draft(
        WorkflowDraftCreateV1(
            request_id=f"{prefix}-create",
            task_definition_id=task_id,
            workflow_id=workflow_id,
            display_name="Demo Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    return authoring.update_draft(
        draft["draft_id"],
        WorkflowDraftReplaceV1(
            request_id=f"{prefix}-save",
            expected_revision=draft["revision"],
            display_name=draft["display_name"],
            roles=minimal_roles(),
            steps=minimal_steps(),
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=1),
    )


def publish(authoring, draft, *, request_id="publish-a"):
    return authoring.publish(
        draft["draft_id"],
        WorkflowPublishV1(
            request_id=request_id,
            expected_revision=draft["revision"],
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=2),
    )


def test_draft_publish_is_atomic_immutable_and_idempotent(tmp_path):
    fleet, authoring = stores(tmp_path)
    draft = create_valid_draft(authoring)
    assert authoring.validate_draft(draft["draft_id"])["valid"] is True

    publication = publish(authoring, draft)
    replay = publish(authoring, draft)
    assert replay["idempotent_replay"] is True
    assert replay["workflow_sha256"] == publication["workflow_sha256"]

    configuration = fleet.get_configuration()
    assert [(item["task_definition_id"], item["version"]) for item in configuration["task_definitions"]] == [("demo-task", 1)]
    registered = OrchestrationStore(fleet.path).get_registry("workflow", "demo-task", 1)
    assert registered["payload_sha256"] == publication["workflow_sha256"]
    assert authoring.list_publications()[0]["source_draft_id"] == draft["draft_id"]
    assert fleet.schema_versions() == list(range(1, 20))

    with pytest.raises(WorkflowAuthoringConflict, match="published Drafts"):
        authoring.update_draft(
            draft["draft_id"],
            WorkflowDraftReplaceV1(
                request_id="save-after-publish",
                expected_revision=draft["revision"] + 1,
                display_name="Changed",
                roles=minimal_roles(),
                steps=minimal_steps(),
            ),
            principal_id="admin-a",
            occurred_at=NOW + timedelta(seconds=3),
        )

    with sqlite3.connect(fleet.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE task_definitions SET display_name = 'changed' WHERE task_definition_id = 'demo-task'"
            )

    with pytest.raises(ConfigurationConflict, match="Workflow Studio"):
        fleet.create_task_definition(
            TaskDefinitionCreate(
                task_definition_id="demo-task",
                version=2,
                display_name="Legacy bypass",
                roles=[
                    {
                        "role": "primary_robot",
                        "asset_type": "robot",
                        "access_mode": "exclusive",
                    }
                ],
            ),
            occurred_at=NOW + timedelta(seconds=4),
        )


def test_concurrent_drafts_cannot_publish_the_same_target_version(tmp_path):
    _, authoring = stores(tmp_path)
    first = create_valid_draft(authoring, prefix="first")
    second = create_valid_draft(authoring, prefix="second")

    assert first["task_version"] == second["task_version"] == 1
    publish(authoring, first, request_id="publish-first")
    with pytest.raises(WorkflowAuthoringConflict, match="next Task version"):
        publish(authoring, second, request_id="publish-second")
    assert authoring.get_draft(second["draft_id"])["state"] == "DRAFT"


def test_invalid_publish_rolls_back_both_registries(tmp_path):
    fleet, authoring = stores(tmp_path)
    draft = authoring.create_draft(
        WorkflowDraftCreateV1(
            request_id="invalid-create",
            task_definition_id="invalid-task",
            display_name="Invalid Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    assert authoring.validate_draft(draft["draft_id"])["valid"] is False
    with pytest.raises(WorkflowAuthoringInvalid):
        publish(authoring, draft, request_id="invalid-publish")
    assert fleet.list_task_definitions() == []
    assert OrchestrationStore(fleet.path).list_registry("workflow") == []
    assert authoring.get_draft(draft["draft_id"])["state"] == "DRAFT"


def test_clone_as_new_version_and_new_task_preserves_workflow(tmp_path):
    _, authoring = stores(tmp_path)
    source = create_valid_draft(authoring)
    publish(authoring, source)

    next_version = authoring.clone_draft(
        WorkflowDraftCloneV1(
            request_id="clone-version",
            source_task_definition_id="demo-task",
            source_task_version=1,
            mode="new_version",
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=3),
    )
    assert (next_version["task_definition_id"], next_version["task_version"]) == ("demo-task", 2)
    assert (next_version["workflow_id"], next_version["workflow_version"]) == ("demo-task", 2)
    assert next_version["steps"][0]["step_id"] == "move_home"
    assert next_version["steps"][0]["capability"] == "move-home@1"

    new_task = authoring.clone_draft(
        WorkflowDraftCloneV1(
            request_id="clone-task",
            source_task_definition_id="demo-task",
            source_task_version=1,
            mode="new_task",
            target_task_definition_id="copied-task",
            display_name="Copied Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=4),
    )
    assert (new_task["task_definition_id"], new_task["task_version"]) == ("copied-task", 1)
    assert (new_task["workflow_id"], new_task["workflow_version"]) == ("copied-task", 1)
    assert new_task["steps"] == next_version["steps"]
    replay = authoring.clone_draft(
        WorkflowDraftCloneV1(
            request_id="clone-task",
            source_task_definition_id="demo-task",
            source_task_version=1,
            mode="new_task",
            target_task_definition_id="copied-task",
            display_name="Copied Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    assert replay["draft_id"] == new_task["draft_id"]
    assert replay["idempotent_replay"] is True


def test_clone_legacy_task_does_not_invent_executable_steps(tmp_path):
    fleet, authoring = stores(tmp_path)
    fleet.create_task_definition(
        TaskDefinitionCreate(
            task_definition_id="legacy-observation",
            version=1,
            display_name="Legacy Observation",
            roles=[
                {
                    "role": "camera",
                    "asset_type": "camera",
                    "access_mode": "shared_read",
                }
            ],
        ),
        occurred_at=NOW,
    )
    clone = authoring.clone_draft(
        WorkflowDraftCloneV1(
            request_id="clone-legacy",
            source_task_definition_id="legacy-observation",
            source_task_version=1,
            mode="new_task",
            target_task_definition_id="legacy-copy",
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert clone["steps"] == []
    assert clone["roles"][0]["requires"] == []
    validation = authoring.validate_draft(clone["draft_id"])
    assert validation["valid"] is False
    assert "requires at least one capability" in validation["issues"][0]["message"]


def test_draft_rejects_secret_and_excessive_nesting(tmp_path):
    _, authoring = stores(tmp_path)
    draft = authoring.create_draft(
        WorkflowDraftCreateV1(
            request_id="secure-create",
            task_definition_id="secure-task",
            display_name="Secure Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    steps = minimal_steps()
    steps[0]["parameters"] = {"api_key": "not-allowed"}
    with pytest.raises(ValueError, match="forbidden field"):
        WorkflowDraftReplaceV1(
            request_id="secure-save",
            expected_revision=draft["revision"],
            display_name="Secure Task",
            roles=minimal_roles(),
            steps=steps,
        )

    for deployment_value in (
        "http://10.0.2.7:5000/command",
        "10.0.2.7",
        "fe80::1",
        "/dev/robot-controller",
    ):
        steps = minimal_steps()
        steps[0]["parameters"] = {"target": deployment_value}
        with pytest.raises(ValueError, match="deployment URI|network address|absolute"):
            WorkflowDraftReplaceV1(
                request_id="deployment-save",
                expected_revision=draft["revision"],
                display_name="Secure Task",
                roles=minimal_roles(),
                steps=steps,
            )

    for secret_key in ("privateKey", "clientSecret", "authToken"):
        steps = minimal_steps()
        steps[0]["parameters"] = {secret_key: "not-allowed"}
        with pytest.raises(ValueError, match="forbidden field"):
            WorkflowDraftReplaceV1(
                request_id="camel-secret-save",
                expected_revision=draft["revision"],
                display_name="Secure Task",
                roles=minimal_roles(),
                steps=steps,
            )

    nested = "value"
    for _ in range(14):
        nested = {"level": nested}
    steps = minimal_steps()
    steps[0]["parameters"] = nested
    with pytest.raises(ValueError, match="maximum JSON depth"):
        WorkflowDraftReplaceV1(
            request_id="deep-save",
            expected_revision=draft["revision"],
            display_name="Secure Task",
            roles=minimal_roles(),
            steps=steps,
        )


def test_skill_catalog_is_versioned_closed_and_bounded():
    catalog, digest = load_skill_catalog(SKILL_CATALOG)
    definitions = {skill.capability: skill for skill in catalog.skills}
    actions = {action.action_ref: action for action in catalog.actions}

    assert catalog.catalog_id == "iitp-scenario1-simulation-skills"
    assert catalog.version == 2
    assert len(digest) == 64
    assert set(definitions) == {
        "simulate-material-delivery@1",
        "simulate-material-return@1",
        "simulate-pick-place@1",
        "simulate-waam-path@1",
    }
    assert set(actions) == {
        "mobile-deliver-base-plate@1",
        "mobile-return-with-base-plate@1",
        "omy-place-on-waam-table@1",
        "omy-return-to-carrier@1",
        "rb5-follow-waam-path@1",
    }
    assert actions["omy-place-on-waam-table@1"].robot_type == "omy-f3m"
    assert actions["omy-place-on-waam-table@1"].script.runtime == "UNITY_CSHARP"
    assert len(actions["omy-place-on-waam-table@1"].script.sha256) == 64
    legacy_catalog, _ = load_skill_catalog(
        WORKSPACE / "contracts/tasks/iitp-scenario1-skill-catalog.v1.json"
    )
    assert legacy_catalog.version == 1
    assert legacy_catalog.actions == []
    delivery = definitions["simulate-material-delivery@1"]
    validate_skill_parameters(
        delivery,
        {"operation": "deliver-base-plate", "speed_scale": 0.75},
    )
    with pytest.raises(ValueError, match="must be <= 1.0"):
        validate_skill_parameters(
            delivery,
            {"operation": "deliver-base-plate", "speed_scale": 1.05},
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_skill_parameters(
            delivery,
            {"operation": "deliver-base-plate", "joint_targets": [0, 1]},
        )


def test_robot_action_catalog_rejects_unsafe_or_ambiguous_entries(tmp_path):
    payload = json.loads(SKILL_CATALOG.read_text(encoding="utf-8"))
    payload["actions"][0]["script"]["script_ref"] = "/tmp/action.py"
    with pytest.raises(ValueError, match="script_ref"):
        SkillCatalogV1.model_validate(payload)

    payload = json.loads(SKILL_CATALOG.read_text(encoding="utf-8"))
    payload["actions"][0]["editable_parameters"].append("operation")
    with pytest.raises(ValueError, match="both fixed and editable"):
        SkillCatalogV1.model_validate(payload)

    _, authoring = stores(tmp_path)
    listed = authoring.list_action_definitions()
    assert [item["action_ref"] for item in listed] == sorted(
        action["action_ref"] for action in payload["actions"]
    )


def test_scenario_skill_speed_is_validated_and_adapter_compatible(tmp_path):
    _, authoring = stores(tmp_path)
    workflow_payload = json.loads(SCENARIO_WORKFLOW.read_text(encoding="utf-8"))
    workflow_payload["steps"][0]["parameters"]["speed_scale"] = 0.75
    profile, _ = load_unity_adapter_profile(ADAPTER_PROFILE)
    draft = authoring.create_draft(
        WorkflowDraftCreateV1(
            request_id="skill-create",
            task_definition_id="skill-task",
            workflow_id="skill-workflow",
            display_name="Skill Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    saved = authoring.update_draft(
        draft["draft_id"],
        WorkflowDraftReplaceV1(
            request_id="skill-save",
            expected_revision=draft["revision"],
            display_name="Skill Task",
            roles=[
                {
                    "role": role.role,
                    "asset_type": "mobile_robot"
                    if role.role == "mobile-carrier"
                    else "robot",
                    "access_mode": role.access,
                    "cardinality": "1",
                    "requires": role.requires,
                }
                for role in profile.roles
            ],
            steps=workflow_payload["steps"],
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert authoring.validate_draft(saved["draft_id"])["valid"] is True
    _, workflow = authoring._compile(saved)
    assert authoring.compatibility_issues(workflow, profile) == []

    invalid_steps = json.loads(json.dumps(workflow_payload["steps"]))
    invalid_steps[0]["parameters"]["speed_scale"] = 1.25
    invalid = authoring.update_draft(
        saved["draft_id"],
        WorkflowDraftReplaceV1(
            request_id="skill-invalid-save",
            expected_revision=saved["revision"],
            display_name="Skill Task",
            roles=saved["roles"],
            steps=invalid_steps,
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=2),
    )
    validation = authoring.validate_draft(invalid["draft_id"])
    assert validation["valid"] is False
    assert "must be <= 1.0" in validation["issues"][0]["message"]


def publish_scenario(authoring):
    workflow = json.loads(SCENARIO_WORKFLOW.read_text(encoding="utf-8"))
    profile, _ = load_unity_adapter_profile(ADAPTER_PROFILE)
    role_asset_types = {
        "mobile-carrier": "mobile_robot",
        "omy-material-handler": "robot",
        "rb5-waam-path": "robot",
    }
    draft = authoring.create_draft(
        WorkflowDraftCreateV1(
            request_id="scenario-create",
            task_definition_id="scenario-task",
            workflow_id=workflow["workflow_id"],
            display_name="Scenario Task",
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    saved = authoring.update_draft(
        draft["draft_id"],
        WorkflowDraftReplaceV1(
            request_id="scenario-save",
            expected_revision=draft["revision"],
            display_name="Scenario Task",
            roles=[
                {
                    "role": role.role,
                    "asset_type": role_asset_types[role.role],
                    "access_mode": role.access,
                    "cardinality": "1",
                    "requires": role.requires,
                }
                for role in profile.roles
            ],
            steps=workflow["steps"],
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=1),
    )
    return publish(authoring, saved, request_id="scenario-publish")


def create_scenario_validation_profile(
    authoring,
    publication,
    *,
    request_id="create-scenario-validation",
    profile_id="scenario-validation",
    required_successful_runs=1,
    max_duration_s=600.0,
    require_reset_acknowledged=True,
):
    return authoring.create_validation_profile(
        SimulationValidationProfileCreateV1(
            request_id=request_id,
            profile_id=profile_id,
            display_name="Scenario 1 simulation acceptance",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            required_successful_runs=required_successful_runs,
            max_duration_s=max_duration_s,
            require_reset_acknowledged=require_reset_acknowledged,
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=3),
    )


def successful_scenario_progress():
    workflow = json.loads(SCENARIO_WORKFLOW.read_text(encoding="utf-8"))
    return {
        "steps": [
            {"step_id": step["step_id"], "state": "SUCCEEDED"}
            for step in workflow["steps"]
        ]
    }


def acknowledge_reset(authoring, run_id, profile_sha256, *, second):
    requested = authoring.request_simulation_control(
        run_id,
        WorkflowSimulationControlV1(
            request_id=f"reset-{run_id}",
            action="RESET",
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=second),
    )
    authoring.worker_heartbeat(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready for Reset",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=second, milliseconds=100),
    )
    claimed = authoring.claim_simulation_control(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        claim_ttl=timedelta(seconds=10),
        occurred_at=NOW + timedelta(seconds=second, milliseconds=200),
        run_id=run_id,
    )
    assert claimed["control_id"] == requested["control_id"]
    authoring.finish_simulation_control(
        claimed["control_id"],
        worker_id="validation-worker",
        state="ACKNOWLEDGED",
        detail="Unity Reset acknowledged",
        occurred_at=NOW + timedelta(seconds=second, milliseconds=300),
    )


def test_simulation_validation_requires_reset_before_immutable_promotion(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    profile = create_scenario_validation_profile(authoring, publication)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=4),
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-profiled-scenario",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Profiled Scenario simulation",
            validation_profile_id=profile["profile_id"],
            validation_profile_version=profile["version"],
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    assert queued["validation"]["state"] == "PENDING"
    claimed = authoring.claim_simulation_run(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=6),
    )
    finished = authoring.finish_simulation_run(
        claimed["run_id"],
        worker_id="validation-worker",
        state="COMPLETE",
        detail="All Scenario 1 steps complete",
        progress=successful_scenario_progress(),
        result={"physical_output_connected": False},
        occurred_at=NOW + timedelta(seconds=10),
    )
    assert finished["validation"]["state"] == "AWAITING_RESET"
    assert next(
        check["detail"] for check in finished["validation"]["checks"]
        if check["code"] == "reset-acknowledged"
    ) == "Simulation Reset acknowledgement is still required"
    assert all(
        check["passed"]
        for check in finished["validation"]["checks"]
        if check["code"] != "reset-acknowledged"
    )

    candidate_body = CommissioningCandidateCreateV1(
        request_id="promote-profiled-scenario",
        profile_id=profile["profile_id"],
        profile_version=profile["version"],
    )
    with pytest.raises(WorkflowAuthoringConflict, match="successful Run count"):
        authoring.create_commissioning_candidate(
            candidate_body,
            principal_id="manager-a",
            occurred_at=NOW + timedelta(seconds=11),
        )

    acknowledge_reset(authoring, queued["run_id"], profile_sha256, second=12)
    validated = authoring.get_simulation_run(
        queued["run_id"],
        occurred_at=NOW + timedelta(seconds=13),
    )
    assert validated["validation"]["state"] == "PASS"
    assert next(
        check["detail"] for check in validated["validation"]["checks"]
        if check["code"] == "reset-acknowledged"
    ) == "Simulation Reset was acknowledged"
    listed_profile = authoring.list_validation_profiles()[0]
    assert listed_profile["passing_run_count"] == 1
    assert listed_profile["qualified"] is True

    candidate = authoring.create_commissioning_candidate(
        candidate_body,
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=14),
    )
    assert candidate["state"] == "SIMULATION_APPROVED"
    assert candidate["evidence_run_ids"] == [queued["run_id"]]
    assert authoring.create_commissioning_candidate(
        candidate_body,
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=15),
    )["idempotent_replay"] is True
    with pytest.raises(WorkflowAuthoringConflict, match="already has"):
        authoring.create_commissioning_candidate(
            CommissioningCandidateCreateV1(
                request_id="duplicate-promotion",
                profile_id=profile["profile_id"],
                profile_version=profile["version"],
            ),
            principal_id="manager-a",
            occurred_at=NOW + timedelta(seconds=16),
        )

    with sqlite3.connect(authoring.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE workflow_simulation_validation_profiles "
                "SET display_name = 'changed' WHERE profile_id = ?",
                (profile["profile_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM workflow_commissioning_candidates "
                "WHERE candidate_id = ?",
                (candidate["candidate_id"],),
            )


def omy_action_draft_body(*, request_id="action-create", source=OMY_PLACE_ACTION_SOURCE):
    return ActionDraftCreateV1(
        request_id=request_id,
        action_id="omy-place-on-waam-table",
        robot_type="omy-f3m",
        robot_label="OMY F3M",
        title="Place material on WAAM table",
        description="Versioned Unity Action source for the OMY transfer step.",
        capability="simulate-pick-place@1",
        asset_type="robot",
        fixed_parameters={"operation": "place-on-waam-table"},
        editable_parameters=["speed_scale"],
        default_timeout_s=300,
        runtime="UNITY_CSHARP",
        source_file_name="OmyPlaceOnWaamTableAction.cs",
        entrypoint="OmyPlaceOnWaamTableAction",
        source_code=source,
    )


def test_action_library_source_is_content_addressed_and_unsafe_code_fails(tmp_path):
    _, authoring = stores(tmp_path)
    unsafe = OMY_PLACE_ACTION_SOURCE.replace(
        "public sealed class",
        "using System.IO;\npublic sealed class",
    )
    draft = authoring.action_library.create_draft(
        omy_action_draft_body(source=unsafe),
        principal_id="admin-a",
        occurred_at=NOW,
    )

    assert "source_code" not in json.dumps(draft)
    assert draft["script"]["size_bytes"] == len(unsafe.encode("utf-8"))
    artifact = (
        tmp_path
        / "action-library"
        / "sources"
        / f'{draft["script"]["sha256"]}.cs'
    )
    assert artifact.read_text(encoding="utf-8") == unsafe

    validated = authoring.action_library.validate_source(
        draft["draft_id"],
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert validated["source_validation_state"] == "FAIL"
    assert "System.IO" in validated["source_validation"]["detail"]
    with pytest.raises(ActionLibraryInvalid, match="Source Gate"):
        authoring.action_library.validate_simulation(
            draft["draft_id"],
            ActionSimulationValidateV1(adapter_id="unity-iitp-scenario1"),
            occurred_at=NOW + timedelta(seconds=2),
        )


def test_action_library_python_is_parsed_but_never_executed_by_central(tmp_path):
    _, authoring = stores(tmp_path)
    safe_source = """def run_simulation(context, parameters):
    return context.apply(parameters)
"""
    draft = authoring.action_library.create_draft(
        ActionDraftCreateV1(
            request_id="python-action-create",
            action_id="omy-python-simulation",
            robot_type="omy-f3m",
            robot_label="OMY F3M",
            title="Python simulation Action",
            description="Output-disconnected Python simulation adapter Action.",
            capability="simulate-pick-place@1",
            asset_type="robot",
            fixed_parameters={"operation": "place-on-waam-table"},
            editable_parameters=["speed_scale"],
            runtime="ROS2_PYTHON",
            source_file_name="omy_python_simulation.py",
            entrypoint="run_simulation",
            source_code=safe_source,
        ),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    validated = authoring.action_library.validate_source(
        draft["draft_id"],
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert validated["source_validation_state"] == "PASS"
    assert validated["source_validation"]["central_executed_source"] is False

    blocked = authoring.action_library.validate_simulation(
        draft["draft_id"],
        ActionSimulationValidateV1(adapter_id="unity-iitp-scenario1"),
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert blocked["simulation_validation_state"] == "FAIL"
    assert "not installed" in blocked["simulation_validation"]["detail"]

    unsafe = authoring.action_library.create_draft(
        ActionDraftCreateV1(
            request_id="unsafe-python-action-create",
            action_id="omy-unsafe-python-simulation",
            robot_type="omy-f3m",
            robot_label="OMY F3M",
            title="Unsafe Python simulation Action",
            description="Fixture that must be rejected by the Source Gate.",
            capability="simulate-pick-place@1",
            asset_type="robot",
            fixed_parameters={"operation": "place-on-waam-table"},
            runtime="ROS2_PYTHON",
            source_file_name="unsafe.py",
            entrypoint="run_simulation",
            source_code="import os\ndef run_simulation(context, parameters):\n    return os.getcwd()\n",
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=3),
    )
    rejected = authoring.action_library.validate_source(
        unsafe["draft_id"],
        occurred_at=NOW + timedelta(seconds=4),
    )
    assert rejected["source_validation_state"] == "FAIL"
    assert "Python import is blocked: os" in rejected["source_validation"]["detail"]


def test_action_library_requires_exact_simulation_evidence_before_publish(tmp_path):
    _, authoring = stores(tmp_path)
    draft = authoring.action_library.create_draft(
        omy_action_draft_body(),
        principal_id="admin-a",
        occurred_at=NOW,
    )
    assert draft["target_version"] == 2
    assert draft["step_id_template"] == "omy_pre_pick_place"

    source_validated = authoring.action_library.validate_source(
        draft["draft_id"],
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert source_validated["source_validation_state"] == "PASS"
    with pytest.raises(ActionLibraryInvalid, match="Simulation Gate"):
        authoring.action_library.publish(
            draft["draft_id"],
            ActionPublishV1(
                request_id="publish-too-early",
                expected_revision=source_validated["revision"],
            ),
            principal_id="admin-a",
            occurred_at=NOW + timedelta(seconds=2),
        )

    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="action-validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=4),
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-action-evidence",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Action evidence simulation",
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    claimed = authoring.claim_simulation_run(
        worker_id="action-validation-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=6),
    )
    assert claimed["run_id"] == queued["run_id"]
    authoring.finish_simulation_run(
        claimed["run_id"],
        worker_id="action-validation-worker",
        state="COMPLETE",
        detail="All Scenario 1 steps complete",
        progress=successful_scenario_progress(),
        result={
            "orchestrator_state": "COMPLETE",
            "physical_output_connected": False,
        },
        occurred_at=NOW + timedelta(seconds=10),
    )

    simulation_validated = authoring.action_library.validate_simulation(
        draft["draft_id"],
        ActionSimulationValidateV1(adapter_id="unity-iitp-scenario1"),
        occurred_at=NOW + timedelta(seconds=11),
    )
    assert simulation_validated["simulation_validation_state"] == "PASS"
    assert (
        simulation_validated["simulation_validation"]["evidence_run_id"]
        == queued["run_id"]
    )

    published = authoring.action_library.publish(
        draft["draft_id"],
        ActionPublishV1(
            request_id="publish-action-v2",
            expected_revision=simulation_validated["revision"],
        ),
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=12),
    )
    assert published["state"] == "PUBLISHED"
    assert "omy-place-on-waam-table@2" in {
        item["action_ref"] for item in authoring.list_action_definitions()
    }

    with sqlite3.connect(authoring.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE action_library_publications SET published_by = 'other'
                WHERE action_ref = 'omy-place-on-waam-table@2'
                """
            )


def test_failed_profiled_simulation_cannot_be_promoted(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    profile = create_scenario_validation_profile(
        authoring,
        publication,
        request_id="create-strict-validation",
        profile_id="strict-validation",
        max_duration_s=2.0,
        require_reset_acknowledged=False,
    )
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=4),
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-failing-scenario",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Failing Scenario simulation",
            validation_profile_id=profile["profile_id"],
            validation_profile_version=profile["version"],
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    authoring.claim_simulation_run(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=6),
    )
    failed_progress = successful_scenario_progress()
    failed_progress["steps"][-1]["state"] = "FAILED"
    finished = authoring.finish_simulation_run(
        queued["run_id"],
        worker_id="validation-worker",
        state="FAILED",
        detail="Fixture failure",
        progress=failed_progress,
        result={"physical_output_connected": False},
        occurred_at=NOW + timedelta(seconds=10),
    )
    assert finished["validation"]["state"] == "FAIL"
    failed_checks = {
        check["code"] for check in finished["validation"]["checks"]
        if not check["passed"]
    }
    assert {"run-complete", "all-steps-succeeded", "duration-within-limit"} <= failed_checks
    with pytest.raises(WorkflowAuthoringConflict, match="successful Run count"):
        authoring.create_commissioning_candidate(
            CommissioningCandidateCreateV1(
                request_id="promote-failed-scenario",
                profile_id=profile["profile_id"],
                profile_version=profile["version"],
            ),
            principal_id="manager-a",
            occurred_at=NOW + timedelta(seconds=11),
        )


def test_profiled_simulation_rejects_partial_step_evidence(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    profile = create_scenario_validation_profile(
        authoring,
        publication,
        request_id="create-partial-evidence-validation",
        profile_id="partial-evidence-validation",
        require_reset_acknowledged=False,
    )
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=4),
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-partial-evidence",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Partial evidence simulation",
            validation_profile_id=profile["profile_id"],
            validation_profile_version=profile["version"],
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    authoring.claim_simulation_run(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=6),
    )
    finished = authoring.finish_simulation_run(
        queued["run_id"],
        worker_id="validation-worker",
        state="COMPLETE",
        detail="Incomplete fixture evidence",
        progress={
            "steps": [
                {"step_id": "mobile_delivery", "state": "SUCCEEDED"},
            ]
        },
        result={"physical_output_connected": False},
        occurred_at=NOW + timedelta(seconds=7),
    )
    assert finished["validation"]["state"] == "FAIL"
    step_check = next(
        check for check in finished["validation"]["checks"]
        if check["code"] == "all-steps-succeeded"
    )
    assert step_check["passed"] is False
    assert "exact Step evidence=False" in step_check["detail"]


def test_terminal_pending_validation_is_reconciled_when_runs_are_read(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    profile = create_scenario_validation_profile(
        authoring,
        publication,
        request_id="create-reconciliation-validation",
        profile_id="reconciliation-validation",
    )
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=4),
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-reconciliation-validation",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Reconciliation simulation",
            validation_profile_id=profile["profile_id"],
            validation_profile_version=profile["version"],
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    authoring.claim_simulation_run(
        worker_id="validation-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=6),
    )
    finished = authoring.finish_simulation_run(
        queued["run_id"],
        worker_id="validation-worker",
        state="COMPLETE",
        detail="Complete fixture evidence",
        progress=successful_scenario_progress(),
        result={"physical_output_connected": False},
        occurred_at=NOW + timedelta(seconds=7),
    )
    assert finished["validation"]["state"] == "AWAITING_RESET"

    with sqlite3.connect(authoring.path) as connection:
        connection.execute(
            """
            UPDATE workflow_simulation_run_validations
            SET state = 'PENDING', checks_json = '[]', evaluated_at = NULL
            WHERE run_id = ?
            """,
            (queued["run_id"],),
        )

    listed = authoring.list_simulation_runs(
        occurred_at=NOW + timedelta(seconds=8)
    )
    reconciled = next(run for run in listed if run["run_id"] == queued["run_id"])
    assert reconciled["validation"]["state"] == "AWAITING_RESET"
    assert next(
        check for check in reconciled["validation"]["checks"]
        if check["code"] == "all-steps-succeeded"
    )["passed"] is True


def test_simulation_queue_pins_publication_and_worker_lease(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=3),
    )
    body = WorkflowSimulationQueueV1(
        request_id="queue-scenario",
        task_definition_id=publication["task_definition_id"],
        task_version=publication["task_version"],
        adapter_id="unity-iitp-scenario1",
        display_name="Scenario simulation",
    )
    queued = authoring.queue_simulation(
        body,
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=4),
    )
    assert queued["state"] == "QUEUED"
    assert queued["workflow_sha256"] == publication["workflow_sha256"]
    assert authoring.queue_simulation(
        body,
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=5),
    )["idempotent_replay"] is True

    claimed = authoring.claim_simulation_run(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=10),
        occurred_at=NOW + timedelta(seconds=5),
    )
    assert claimed["state"] == "RUNNING"
    progress = {"steps": [{"step_id": "mobile_delivery", "state": "DISPATCHED"}]}
    authoring.update_simulation_progress(
        claimed["run_id"],
        worker_id="unity-worker",
        progress=progress,
        detail="Running",
        lease_ttl=timedelta(seconds=10),
        occurred_at=NOW + timedelta(seconds=6),
    )
    finished = authoring.finish_simulation_run(
        claimed["run_id"],
        worker_id="unity-worker",
        state="COMPLETE",
        detail="Complete",
        progress={"steps": [{"step_id": "mobile_delivery", "state": "SUCCEEDED"}]},
        result={"physical_output_connected": False},
        occurred_at=NOW + timedelta(seconds=7),
    )
    assert finished["state"] == "COMPLETE"
    assert finished["result"]["physical_output_connected"] is False
    with sqlite3.connect(authoring.path) as connection:
        event_states = [
            row[0]
            for row in connection.execute(
                """
                SELECT state FROM workflow_simulation_run_events
                WHERE run_id = ? ORDER BY id
                """,
                (claimed["run_id"],),
            ).fetchall()
        ]
    assert event_states == ["QUEUED", "RUNNING", "RUNNING", "COMPLETE"]


def test_simulation_rejects_untrusted_workers_and_never_requeues_expired_run(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    body = WorkflowSimulationQueueV1(
        request_id="queue-guarded",
        task_definition_id=publication["task_definition_id"],
        task_version=publication["task_version"],
        adapter_id="unity-iitp-scenario1",
        display_name="Guarded simulation",
    )

    with pytest.raises(WorkflowAuthoringInvalid, match="digest"):
        authoring.worker_heartbeat(
            worker_id="wrong-profile",
            adapter_id="unity-iitp-scenario1",
            profile_sha256="0" * 64,
            scenario_id="iitp-scenario1",
            ready=True,
            detail="Wrong profile",
            physical_output_connected=False,
            occurred_at=NOW,
        )
    with pytest.raises(WorkflowAuthoringInvalid, match="physical outputs"):
        authoring.worker_heartbeat(
            worker_id="output-connected",
            adapter_id="unity-iitp-scenario1",
            profile_sha256=profile_sha256,
            scenario_id="iitp-scenario1",
            ready=True,
            detail="Unsafe Worker",
            physical_output_connected=True,
            occurred_at=NOW,
        )

    authoring.worker_heartbeat(
        worker_id="stale-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW,
    )
    with pytest.raises(WorkflowAuthoringUnavailable, match="fresh"):
        authoring.queue_simulation(
            body,
            principal_id="admin-a",
            occurred_at=NOW + timedelta(seconds=6),
        )

    authoring.worker_heartbeat(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=7),
    )
    queued = authoring.queue_simulation(
        body,
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=8),
    )
    with pytest.raises(WorkflowAuthoringUnavailable, match="heartbeat"):
        authoring.claim_simulation_run(
            worker_id="unity-worker",
            adapter_id="unity-iitp-scenario1",
            lease_ttl=timedelta(seconds=3),
            occurred_at=NOW + timedelta(seconds=13),
        )

    authoring.worker_heartbeat(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=13),
    )
    claimed = authoring.claim_simulation_run(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=3),
        occurred_at=NOW + timedelta(seconds=13),
    )
    with pytest.raises(WorkflowAuthoringConflict, match="no longer owns"):
        authoring.update_simulation_progress(
            claimed["run_id"],
            worker_id="unity-worker",
            progress={"steps": []},
            detail="Late renewal",
            lease_ttl=timedelta(seconds=3),
            occurred_at=NOW + timedelta(seconds=17),
        )
    reconciling = authoring.get_simulation_run(
        claimed["run_id"], occurred_at=NOW + timedelta(seconds=17)
    )
    assert reconciling["state"] == "RUNNING"
    assert reconciling["reconciliation_required"] is True
    reclaimed = authoring.claim_simulation_run(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=3),
        occurred_at=NOW + timedelta(seconds=17),
    )
    assert reclaimed["run_id"] == claimed["run_id"]
    assert reclaimed["reconciliation"] is True
    assert authoring.get_simulation_run(
        queued["run_id"], occurred_at=NOW + timedelta(seconds=17)
    )["state"] == "RUNNING"


def test_simulation_retry_replays_before_adapter_lookup(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="unity-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW + timedelta(seconds=3),
    )
    body = WorkflowSimulationQueueV1(
        request_id="queue-replay",
        task_definition_id=publication["task_definition_id"],
        task_version=publication["task_version"],
        adapter_id="unity-iitp-scenario1",
        display_name="Replay simulation",
    )
    first = authoring.queue_simulation(
        body,
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=4),
    )
    authoring.adapter_profiles.clear()
    replay = authoring.queue_simulation(
        body,
        principal_id="admin-a",
        occurred_at=NOW + timedelta(seconds=5),
    )
    assert replay["run_id"] == first["run_id"]
    assert replay["idempotent_replay"] is True


def test_simulation_controls_are_idempotent_audited_and_terminal(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="operations-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW,
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-operations",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Operations simulation",
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=1),
    )
    authoring.claim_simulation_run(
        worker_id="operations-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=30),
        occurred_at=NOW + timedelta(seconds=2),
    )

    def apply(action: str, request_id: str, second: int):
        body = WorkflowSimulationControlV1(
            request_id=request_id,
            action=action,
        )
        requested = authoring.request_simulation_control(
            queued["run_id"],
            body,
            principal_id="manager-a",
            occurred_at=NOW + timedelta(seconds=second),
        )
        replay = authoring.request_simulation_control(
            queued["run_id"],
            body,
            principal_id="manager-a",
            occurred_at=NOW + timedelta(seconds=second, milliseconds=100),
        )
        assert replay["control_id"] == requested["control_id"]
        assert replay["idempotent_replay"] is True
        authoring.worker_heartbeat(
            worker_id="operations-worker",
            adapter_id="unity-iitp-scenario1",
            profile_sha256=profile_sha256,
            scenario_id="iitp-scenario1",
            ready=False,
            detail="Unity Run active",
            physical_output_connected=False,
            occurred_at=NOW + timedelta(seconds=second, milliseconds=150),
        )
        claimed = authoring.claim_simulation_control(
            worker_id="operations-worker",
            adapter_id="unity-iitp-scenario1",
            claim_ttl=timedelta(seconds=10),
            occurred_at=NOW + timedelta(seconds=second, milliseconds=200),
            run_id=queued["run_id"],
        )
        assert claimed["action"] == action
        return authoring.finish_simulation_control(
            claimed["control_id"],
            worker_id="operations-worker",
            state="ACKNOWLEDGED",
            detail=f"Unity acknowledged {action}",
            occurred_at=NOW + timedelta(seconds=second, milliseconds=300),
        )

    apply("HOLD", "hold-operations", 3)
    assert authoring.get_simulation_run(
        queued["run_id"], occurred_at=NOW + timedelta(seconds=3, milliseconds=400)
    )["progress"]["lifecycle_state"] == "HELD"
    apply("RESUME", "resume-operations", 4)
    apply("ABORT", "abort-operations", 5)
    interrupted = authoring.get_simulation_run(
        queued["run_id"], occurred_at=NOW + timedelta(seconds=5, milliseconds=400)
    )
    assert interrupted["state"] == "INTERRUPTED"
    assert interrupted["progress"]["lifecycle_state"] == "ABORTED"

    apply("RESET", "reset-operations", 6)
    reset = authoring.get_simulation_run(
        queued["run_id"], occurred_at=NOW + timedelta(seconds=6, milliseconds=400)
    )
    assert reset["progress"]["lifecycle_state"] == "READY"
    assert [item["action"] for item in reset["controls"]] == [
        "HOLD",
        "RESUME",
        "ABORT",
        "RESET",
    ]


def test_hold_preserves_restart_reconciliation_until_worker_reclaims(tmp_path):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    authoring.worker_heartbeat(
        worker_id="recovery-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=NOW,
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-recovery-hold",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Recovery hold simulation",
        ),
        principal_id="manager-a",
        occurred_at=NOW + timedelta(seconds=1),
    )
    authoring.claim_simulation_run(
        worker_id="recovery-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=3),
        occurred_at=NOW + timedelta(seconds=2),
    )
    expired_at = NOW + timedelta(seconds=6)
    expired = authoring.get_simulation_run(queued["run_id"], occurred_at=expired_at)
    assert expired["worker_id"] is None
    assert expired["reconciliation_required"] is True

    authoring.worker_heartbeat(
        worker_id="recovery-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=False,
        detail="Unity Run active",
        physical_output_connected=False,
        occurred_at=expired_at,
    )
    requested = authoring.request_simulation_control(
        queued["run_id"],
        WorkflowSimulationControlV1(
            request_id="hold-during-reconciliation",
            action="HOLD",
        ),
        principal_id="manager-a",
        occurred_at=expired_at + timedelta(milliseconds=100),
    )
    control = authoring.claim_simulation_control(
        worker_id="recovery-worker",
        adapter_id="unity-iitp-scenario1",
        claim_ttl=timedelta(seconds=10),
        occurred_at=expired_at + timedelta(milliseconds=200),
        run_id=queued["run_id"],
    )
    assert control["control_id"] == requested["control_id"]
    authoring.finish_simulation_control(
        control["control_id"],
        worker_id="recovery-worker",
        state="ACKNOWLEDGED",
        detail="Simulation held",
        occurred_at=expired_at + timedelta(milliseconds=300),
    )
    held = authoring.get_simulation_run(
        queued["run_id"], occurred_at=expired_at + timedelta(milliseconds=400)
    )
    assert held["progress"]["lifecycle_state"] == "HELD"
    assert held["reconciliation_required"] is True

    reclaimed = authoring.claim_simulation_run(
        worker_id="recovery-worker",
        adapter_id="unity-iitp-scenario1",
        lease_ttl=timedelta(seconds=3),
        occurred_at=expired_at + timedelta(milliseconds=500),
    )
    assert reclaimed["run_id"] == queued["run_id"]
    assert reclaimed["reconciliation"] is True


def test_unity_bridge_root_cannot_escape_output_isolated_tmp_namespace(tmp_path):
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"bridge_root": str(tmp_path)}), encoding="utf-8")
    with pytest.raises(ValueError, match="/tmp/sang-unity"):
        _bridge_root("/tmp/sang-unity-default", marker)

    marker.write_text(
        json.dumps({"bridge_root": "/tmp/sang-unity-workflow-test"}),
        encoding="utf-8",
    )
    assert _bridge_root("/tmp/sang-unity-default", marker) == Path(
        "/tmp/sang-unity-workflow-test"
    )


def test_worker_records_non_complete_orchestrator_result_as_failed(
    tmp_path, monkeypatch
):
    _, authoring = stores(tmp_path)
    publication = publish_scenario(authoring)
    _, profile_sha256 = load_unity_adapter_profile(ADAPTER_PROFILE)
    current = datetime.now(timezone.utc)
    authoring.worker_heartbeat(
        worker_id="held-worker",
        adapter_id="unity-iitp-scenario1",
        profile_sha256=profile_sha256,
        scenario_id="iitp-scenario1",
        ready=True,
        detail="Unity ready",
        physical_output_connected=False,
        occurred_at=current,
    )
    queued = authoring.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="queue-held",
            task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"],
            adapter_id="unity-iitp-scenario1",
            display_name="Held simulation",
        ),
        principal_id="admin-a",
        occurred_at=current,
    )
    worker = UnityWorkflowWorker(
        store=authoring,
        worker_id="held-worker",
        adapter_profile_path=ADAPTER_PROFILE,
        scenario_config_path=WORKSPACE / "config/unity_scenario1_simulation.yaml",
        runtime_root=tmp_path / "worker-runtime",
        marker_path=tmp_path / "unused-marker",
        lease_ttl=timedelta(seconds=10),
    )
    backend = SimpleNamespace(
        bridge_root=Path("/tmp/sang-unity-worker-test"),
        assert_ready=lambda: SimpleNamespace(observed_phase="ReadyDelivery"),
        read_heartbeat=lambda: SimpleNamespace(
            ready=True,
            lifecycle_state="READY",
            observed_phase="ReadyDelivery",
            current_run_id=None,
            active_command_id=None,
            updated_at=current,
        ),
    )
    monkeypatch.setattr(worker, "_backend", lambda **_: backend)
    monkeypatch.setattr(
        workflow_worker_module,
        "execute_scenario",
        lambda **_: {
            "run": {"state": "HELD", "reason": "fixture Agent failed"},
            "physical_output_connected": False,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    assert worker.run_once() is True
    result = authoring.get_simulation_run(
        queued["run_id"], occurred_at=datetime.now(timezone.utc)
    )
    assert result["state"] == "FAILED"
    assert result["result"]["orchestrator_state"] == "HELD"
    assert "fixture Agent failed" in result["detail"]


def test_workflow_authoring_api_is_served_and_bounds_body(tmp_path):
    path = tmp_path / "fleet.db"
    app = create_app(
        FleetStore(path),
        auth_enabled=False,
        workflow_adapter_profile_paths=[ADAPTER_PROFILE],
        workflow_skill_catalog_paths=[SKILL_CATALOG],
    )
    client = TestClient(app)
    skill_response = client.get("/api/v1/workflow-authoring/skill-definitions")
    assert skill_response.status_code == 200
    assert len(skill_response.json()) == 4
    assert skill_response.json()[0]["parameter_schema"]["additionalProperties"] is False
    action_response = client.get("/api/v1/workflow-authoring/action-definitions")
    assert action_response.status_code == 200
    assert len(action_response.json()) == 5
    assert action_response.json()[0]["script"]["runtime"] == "UNITY_CSHARP"
    response = client.post(
        "/api/v1/workflow-authoring/drafts",
        json={
            "request_id": "api-create",
            "task_definition_id": "api-task",
            "display_name": "API Task",
        },
    )
    assert response.status_code == 201
    assert client.get("/api/v1/workflow-authoring/drafts").json()[0]["draft_id"] == response.json()["draft_id"]
    deployment = client.put(
        f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}",
        json={
            "request_id": "api-deployment-save",
            "expected_revision": response.json()["revision"],
            "display_name": "API Task",
            "roles": minimal_roles(),
            "steps": [
                {
                    **minimal_steps()[0],
                    "parameters": {"target": "http://10.0.2.7/command"},
                }
            ],
        },
    )
    assert deployment.status_code == 422
    for index, rejected_parameters in enumerate(
        (
            {"clientSecret": "not-allowed"},
            {"authToken": "not-allowed"},
            {"clientCredentials": "plaintext-secret"},
            {"apiKeys": ["not-allowed"]},
            {"privateKeys": ["not-allowed"]},
            {"accessKeys": ["not-allowed"]},
            {"signingKeys": ["not-allowed"]},
            {"networkAddresses": ["controller.invalid"]},
            {"APIKey": "not-allowed"},
            {"APIKeys": ["not-allowed"]},
            {"client.secret": "not-allowed"},
            {"private.keys": ["not-allowed"]},
            {"network.addresses": ["controller.invalid"]},
            {"auth:token": "not-allowed"},
            {"workingDirectory": "/"},
            {"runtimeRoot": "/"},
            {"mountPoint": "/"},
            {"workingDirectory": "//"},
            {"runtimeRoot": "///"},
            {"mountPoint": "\\"},
            {"target": "fe80::1"},
        )
    ):
        rejected = client.put(
            f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}",
            json={
                "request_id": f"api-parameter-reject-{index}",
                "expected_revision": response.json()["revision"],
                "display_name": "API Task",
                "roles": minimal_roles(),
                "steps": [
                    {**minimal_steps()[0], "parameters": rejected_parameters}
                ],
            },
        )
        assert rejected.status_code == 422
    for index, rejected_description in enumerate(
        (
            "fe80::1",
            "Controller is fe80::1.",
            "Controller[fe80::1].",
            "Device path=/dev/ttyUSB0",
            r"Device path=C:\robot\config",
            'Device "/dev/ttyUSB0"',
            "Device [/opt/robot/config.yaml]",
            r'Device "C:\robot\config"',
            r'Device "\\server\share\config"',
            "Device `/dev/ttyUSB0`",
            "Device |/opt/robot/config.yaml",
            "Device @/var/lib/robot",
            r"Device `C:\robot\config`",
            r"Device |\\server\share\config",
            "artifact://safe/item then https://controller.invalid/api",
        )
    ):
        rejected = client.put(
            f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}",
            json={
                "request_id": f"api-description-reject-{index}",
                "expected_revision": response.json()["revision"],
                "display_name": "API Task",
                "description": rejected_description,
                "roles": minimal_roles(),
                "steps": minimal_steps(),
            },
        )
        assert rejected.status_code == 422
    persisted = client.get(
        f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}"
    ).json()
    assert persisted["revision"] == response.json()["revision"]
    assert persisted["description"] is None
    assert persisted["roles"] == []
    assert persisted["steps"] == []
    allowed_reference = client.put(
        f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}",
        json={
            "request_id": "api-allowed-reference-save",
            "expected_revision": response.json()["revision"],
            "display_name": "API Task",
            "description": "Use artifact://safe/item",
            "roles": minimal_roles(),
            "steps": [
                {
                    **minimal_steps()[0],
                    "parameters": {
                        "motion_ref": "artifact://safe/item",
                        "APIVersion": "1",
                        "ratio": "1/2",
                        "route_label": "A/B",
                    },
                }
            ],
        },
    )
    assert allowed_reference.status_code == 200
    oversized = client.post(
        "/api/v1/workflow-authoring/drafts",
        content=b"x" * (512 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    spoofed = client.put(
        f"/api/v1/workflow-authoring/drafts/{response.json()['draft_id']}",
        content=json.dumps(
            {
                "request_id": "spoofed-size",
                "expected_revision": response.json()["revision"],
                "display_name": "API Task",
                "description": "x" * (512 * 1024),
                "roles": [],
                "steps": [],
            }
        ),
        headers={"content-length": "1", "content-type": "application/json"},
    )
    assert spoofed.status_code in {400, 413}
    missing_length = client.post(
        "/api/v1/workflow-authoring/drafts",
        content=(chunk for chunk in [b"{}"]),
        headers={"content-type": "application/json"},
    )
    assert missing_length.status_code == 411


def test_action_library_api_keeps_source_private_and_blocks_early_publish(tmp_path):
    path = tmp_path / "fleet.db"
    app = create_app(
        FleetStore(path),
        auth_enabled=False,
        workflow_adapter_profile_paths=[ADAPTER_PROFILE],
        workflow_skill_catalog_paths=[SKILL_CATALOG],
    )
    client = TestClient(app)
    payload = omy_action_draft_body(request_id="api-action-create").model_dump(
        mode="json"
    )
    created = client.post(
        "/api/v1/workflow-authoring/action-library/drafts",
        json=payload,
    )
    assert created.status_code == 201
    draft = created.json()
    assert "source_code" not in json.dumps(draft)

    listed = client.get("/api/v1/workflow-authoring/action-library")
    assert listed.status_code == 200
    assert len(listed.json()) == 6
    validated = client.post(
        f'/api/v1/workflow-authoring/action-library/drafts/{draft["draft_id"]}/source-validation',
        json={},
    )
    assert validated.status_code == 200
    assert validated.json()["source_validation_state"] == "PASS"

    blocked = client.post(
        f'/api/v1/workflow-authoring/action-library/drafts/{draft["draft_id"]}/publish',
        json={
            "request_id": "api-action-publish-too-early",
            "expected_revision": validated.json()["revision"],
        },
    )
    assert blocked.status_code == 422
    assert "Simulation Gate" in blocked.json()["detail"]
