from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fleet_control_plane.control_contracts import ExecuteSkillRequestV1
from fleet_control_plane.orchestration_store import OrchestrationStore, payload_digest
from fleet_control_plane.store import FleetStore
from fleet_control_plane.unity_dynamic_binding import resolve_unity_workflow
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBoundCommandV2,
    UnityBoundResultV2,
    UnityBridgeError,
    UnityRoleBindingV1,
    UnityRunAborted,
    UnityScenarioConfigV1,
    UnityScenarioFileBackend,
    _write_json_atomic,
    execute_scenario,
)
from fleet_control_plane.workflow_authoring import (
    UnityAdapterProfileV2,
    WorkflowAuthoringConflict,
    WorkflowAuthoringStore,
    WorkflowAuthoringUnavailable,
    WorkflowDraftCreateV1,
    WorkflowDraftReplaceV1,
    WorkflowPublishV1,
    WorkflowSimulationQueueV1,
    load_unity_adapter_profile,
)
import fleet_control_plane.workflow_simulation_worker as worker_module

from test_unity_dynamic_binding import ROOT, _fixture


@pytest.fixture
def execution(tmp_path):
    contract, workflow, heartbeat = _fixture()
    _, dispatches = resolve_unity_workflow(workflow, contract)
    selected = dispatches["custom-step"]
    backend = UnityScenarioFileBackend(
        scenario_id=contract.scene.scene_id,
        bridge_root=tmp_path / "bridge",
        allowed_steps={"custom-step": selected.action.capability},
        heartbeat_max_age_s=60,
        result_timeout_s=0.2,
        poll_interval_s=0.001,
        dynamic_binding=contract,
        dispatches=dispatches,
        abort_event=Event(),
    )
    _write_json_atomic(backend.heartbeat_path, heartbeat)
    config = UnityScenarioConfigV1(
        scenario_id=contract.scene.scene_id,
        workflow_path="unused.json",
        bridge_root=str(backend.bridge_root),
        heartbeat_max_age_s=60,
        result_timeout_s=0.2,
        roles={"my-handler": UnityRoleBindingV1(
            asset_id=selected.binding.identity.asset_id,
            agent_id=selected.agent_id,
        )},
    )
    request = SimpleNamespace(
        execution_mode="SIMULATION",
        step_id="custom-step",
        capability_ref=selected.action.capability,
        agent_id=selected.agent_id,
        command_id="regression-command",
        run_id="regression-run",
        parameters={**selected.parameters, "bound_asset_id": selected.binding.identity.asset_id},
        goal=SimpleNamespace(
            asset_id=selected.binding.identity.asset_id,
            not_valid_after=datetime.now(timezone.utc) + timedelta(minutes=1),
        ),
    )
    return SimpleNamespace(
        contract=contract, workflow=workflow, selected=selected,
        backend=backend, config=config, request=request,
    )


def test_real_execute_scenario_injects_asset_metadata_but_emits_clean_action_inputs(
    execution, tmp_path, monkeypatch
):
    backend = execution.backend
    wait_for_result = backend._wait_for_result
    requests = []

    def complete_local_command(request, result_path):
        requests.append(request)
        command = UnityBoundCommandV2.model_validate_json(
            (backend.commands_dir / f"{request.command_id}.json").read_text()
        )
        fields = (
            "scenario_id", "command_id", "run_id", "step_id", "unity_binding_id",
            "controller_component_binding_id", "action_key",
            "scene_manifest_sha256", "action_source_sha256",
        )
        result = UnityBoundResultV2(
            **{name: getattr(command, name) for name in fields},
            state="SUCCEEDED", detail="Isolated test completion",
            observed_phase="Complete", completed_at=datetime.now(timezone.utc),
        )
        _write_json_atomic(result_path, result.model_dump(mode="json"))
        return wait_for_result(request, result_path)

    monkeypatch.setattr(backend, "_wait_for_result", complete_local_command)
    summary = execute_scenario(
        config=execution.config, workflow=execution.workflow, backend=backend,
        runtime_dir=tmp_path / "runtime", run_id="signed-dynamic-run",
    )

    assert summary["run"]["state"] == "COMPLETE"
    assert summary["physical_output_connected"] is False
    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, ExecuteSkillRequestV1)
    assert request.parameters == {
        **execution.selected.parameters,
        "bound_asset_id": execution.selected.binding.identity.asset_id,
    }
    commands = list(backend.commands_dir.glob("*.json"))
    assert len(commands) == 1
    command = json.loads(commands[0].read_text())
    assert command["schema_version"] == 2
    assert command["parameters"] == execution.selected.parameters
    assert command["action_key"] == execution.selected.action.action_ref
    assert command["action_source_sha256"] == execution.selected.action.script.sha256
    assert command["physical_output_connected"] is False


@pytest.mark.parametrize("change", ["missing", "wrong-metadata", "wrong-goal", "wrong-both"])
def test_reserved_asset_metadata_must_match_goal_and_resolved_asset(execution, change):
    request = execution.request
    if change == "missing":
        request.parameters.pop("bound_asset_id")
    if change in {"wrong-metadata", "wrong-both"}:
        request.parameters["bound_asset_id"] = "different-asset"
    if change in {"wrong-goal", "wrong-both"}:
        request.goal.asset_id = "different-asset"
    with pytest.raises(UnityBridgeError, match="resolved Unity"):
        execution.backend._command_for(request, require_ready=False)
    assert not list(execution.backend.commands_dir.glob("*.json"))


@pytest.mark.parametrize("method", ["execute_skill", "recover_skill"])
@pytest.mark.parametrize("abort_at", ["before-call", "emission-fence"])
def test_abort_fences_execute_and_recover_before_command_write(execution, method, abort_at):
    backend = execution.backend
    if abort_at == "before-call":
        backend.abort_event.set()
    else:
        @contextmanager
        def lose_ownership_at_emission():
            backend.abort_event.set()
            yield

        backend.command_fence = lose_ownership_at_emission
    with pytest.raises(UnityRunAborted):
        getattr(backend, method)(execution.request)
    assert not list(backend.commands_dir.glob("*.json"))


@pytest.mark.parametrize("failure", ["renewal-conflict", "renewal-unavailable", "dispatch-deadline"])
def test_worker_latches_lease_loss_and_fences_further_emission(
    execution, tmp_path, monkeypatch, failure
):
    legacy, _ = load_unity_adapter_profile(
        ROOT / "contracts/tasks/iitp-scenario1-unity-adapter.v1.json"
    )
    profile = UnityAdapterProfileV2.model_validate({
        **legacy.model_dump(mode="json"),
        "schema_version": 2, "dynamic_binding": execution.contract,
    })
    profile_sha256 = payload_digest(profile.model_dump(mode="json"))
    store = Mock(spec=WorkflowAuthoringStore)
    store.claim_simulation_run.return_value = {
        "run_id": execution.request.run_id, "reconciliation": False,
        "profile_sha256": profile_sha256,
        "workflow": execution.workflow.model_dump(mode="json"), "progress": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    store.claim_simulation_control.return_value = None
    store.compatibility_issues.return_value = []
    store.simulation_dispatch_claim.side_effect = lambda *args, **kwargs: nullcontext()
    clock = [100.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    renewal_attempted = Event()

    def fail_renewal(*args, **kwargs):
        renewal_attempted.set()
        if failure == "renewal-conflict":
            raise WorkflowAuthoringConflict("Worker no longer owns the Simulation Run")
        clock[0] = 103.01
        raise OSError("Renewal store unavailable")

    store.update_simulation_progress.side_effect = fail_renewal
    if failure == "dispatch-deadline":
        monkeypatch.setattr(worker_module, "Thread", Mock(return_value=Mock()))

    # Constructor/profile loading is separate; exercise the actual run/renewal loop.
    worker = worker_module.UnityWorkflowWorker.__new__(worker_module.UnityWorkflowWorker)
    worker.store = store
    worker.worker_id = "regression-worker"
    worker.profile = profile
    worker.profile_sha256 = profile_sha256
    worker.config = execution.config
    worker.runtime_root = tmp_path / "worker-runtime"
    worker.lease_ttl = timedelta(seconds=3)

    def backend_for_run(*, abort_event):
        execution.backend.abort_event = abort_event
        return execution.backend

    monkeypatch.setattr(worker, "_backend", backend_for_run)
    observed = {}

    def attempt_after_lease_loss(**kwargs):
        backend = kwargs["backend"]
        if failure == "dispatch-deadline":
            clock[0] = 103.01
        else:
            observed["renewal_attempted"] = renewal_attempted.wait(2)
            observed["renewal_latched_abort"] = backend.abort_event.wait(2)
        errors = []
        for method in ("execute_skill", "recover_skill"):
            try:
                getattr(backend, method)(execution.request)
            except Exception as exc:
                errors.append(exc)
            else:
                errors.append(None)
        observed["errors"] = errors
        return {"run": {"state": "COMPLETE"}, "physical_output_connected": False,
                "completed_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(worker_module, "execute_scenario", attempt_after_lease_loss)
    assert worker.run_once() is True
    if failure != "dispatch-deadline":
        assert observed["renewal_attempted"]
        assert observed["renewal_latched_abort"]
        store.update_simulation_progress.assert_called_once()
    else:
        store.update_simulation_progress.assert_not_called()
    assert len(observed["errors"]) == 2
    assert all(isinstance(error, UnityRunAborted) for error in observed["errors"])
    assert execution.backend.abort_event.is_set()
    assert not list(execution.backend.commands_dir.glob("*.json"))
    store.simulation_dispatch_claim.assert_called_with(
        execution.request.run_id, worker_id=worker.worker_id
    )
    store.finish_simulation_run.assert_not_called()


@pytest.mark.parametrize("reconciling", [False, True], ids=["queued", "reconciling"])
def test_profile_rollover_cannot_claim_a_run_pinned_to_another_asset(
    execution, tmp_path, reconciling
):
    legacy, _ = load_unity_adapter_profile(
        ROOT / "contracts/tasks/iitp-scenario1-unity-adapter.v1.json"
    )
    original = UnityAdapterProfileV2.model_validate({
        **legacy.model_dump(mode="json"),
        "schema_version": 2, "dynamic_binding": execution.contract,
    })
    raw = original.model_dump(mode="json", by_alias=True)
    binding = raw["dynamic_binding"]["scene"]["bindings"][0]
    for key in ("asset_id", "global_asset_id", "aas_id"):
        binding["identity"][key] += "-replacement"
    binding["unity_binding_id"] += "-replacement"
    raw["dynamic_binding"]["assets"][0].update(
        unity_binding_id=binding["unity_binding_id"], agent_id="replacement-agent"
    )
    replacement = UnityAdapterProfileV2.model_validate(raw)
    original_digest = payload_digest(original.model_dump(mode="json"))
    replacement_digest = payload_digest(replacement.model_dump(mode="json"))
    assert original_digest != replacement_digest

    database = tmp_path / "rollover.db"
    FleetStore(database)
    OrchestrationStore(database)
    store = WorkflowAuthoringStore(database, skill_catalog_paths=[
        ROOT / "contracts/tasks/iitp-scenario1-skill-catalog.v2.json"
    ])
    now = datetime.now(timezone.utc)

    def configure(profile, digest, worker_id, occurred_at):
        # Simulate a configured-profile reload, without changing the persisted Run.
        store.adapter_profiles[profile.adapter_id] = (profile, digest)
        store.worker_heartbeat(
            worker_id=worker_id, adapter_id=profile.adapter_id,
            profile_sha256=digest, scenario_id=profile.scenario_id,
            ready=True, detail="Isolated rollover regression",
            physical_output_connected=False, occurred_at=occurred_at,
        )

    configure(original, original_digest, "original-worker", now)
    draft = store.create_draft(
        WorkflowDraftCreateV1(request_id="rollover-create", task_definition_id="rollover-task",
                              display_name="Rollover Task"),
        principal_id="regression-admin", occurred_at=now,
    )
    draft = store.update_draft(
        draft["draft_id"],
        WorkflowDraftReplaceV1(
            request_id="rollover-save", expected_revision=draft["revision"],
            display_name="Rollover Task",
            roles=[dict(role="my-handler", asset_type="robot", requires=["simulate-pick-place@1"])],
            steps=[step.model_dump(mode="json") for step in execution.workflow.steps],
        ),
        principal_id="regression-admin", occurred_at=now,
    )
    publication = store.publish(
        draft["draft_id"],
        WorkflowPublishV1(request_id="rollover-publish", expected_revision=draft["revision"]),
        principal_id="regression-admin", occurred_at=now,
    )
    queued = store.queue_simulation(
        WorkflowSimulationQueueV1(
            request_id="rollover-queue", task_definition_id=publication["task_definition_id"],
            task_version=publication["task_version"], adapter_id=original.adapter_id,
            display_name="Pinned Run",
        ),
        principal_id="regression-admin", occurred_at=now,
    )
    if reconciling:
        claimed = store.claim_simulation_run(
            worker_id="original-worker", adapter_id=original.adapter_id,
            lease_ttl=timedelta(seconds=3), occurred_at=now,
        )
        assert claimed["run_id"] == queued["run_id"]
    rollover_at = now + timedelta(seconds=4)
    configure(replacement, replacement_digest, "replacement-worker", rollover_at)
    assert store.compatibility_issues(execution.workflow, replacement) == []
    _, replacement_actions = resolve_unity_workflow(
        execution.workflow, replacement.dynamic_binding
    )
    assert (replacement_actions["custom-step"].binding.identity.asset_id
            != execution.selected.binding.identity.asset_id)

    before = store.get_simulation_run(queued["run_id"], occurred_at=rollover_at)
    assert before["reconciliation_required"] is reconciling
    with pytest.raises(WorkflowAuthoringUnavailable, match="different adapter profile"):
        store.claim_simulation_run(
            worker_id="replacement-worker", adapter_id=replacement.adapter_id,
            lease_ttl=timedelta(seconds=3), occurred_at=rollover_at,
        )
    assert store.get_simulation_run(queued["run_id"], occurred_at=rollover_at) == before

    configure(original, original_digest, "original-worker", rollover_at)
    claimed = store.claim_simulation_run(
        worker_id="original-worker", adapter_id=original.adapter_id,
        lease_ttl=timedelta(seconds=3), occurred_at=rollover_at,
    )
    assert claimed["run_id"] == queued["run_id"]
    assert claimed["profile_sha256"] == original_digest
    assert claimed["reconciliation"] is reconciling


def test_worker_unavailable_claim_reports_not_ready_without_dispatch(execution, monkeypatch):
    detail = "Simulation Run pins a different adapter profile; restore its deployment before claiming"
    store = Mock(spec=WorkflowAuthoringStore)
    store.claim_simulation_control.return_value = None
    store.claim_simulation_run.side_effect = WorkflowAuthoringUnavailable(detail)
    worker = worker_module.UnityWorkflowWorker.__new__(worker_module.UnityWorkflowWorker)
    worker.store = store
    worker.worker_id = "blocked-worker"
    worker.profile = SimpleNamespace(
        adapter_id="unity-iitp-scenario1", scenario_id=execution.config.scenario_id
    )
    worker.profile_sha256 = "0" * 64
    worker.lease_ttl = timedelta(seconds=3)
    monkeypatch.setattr(worker, "_backend", Mock(return_value=execution.backend))
    execute = Mock()
    monkeypatch.setattr(worker_module, "execute_scenario", execute)

    assert worker.run_once() is False
    store.claim_simulation_run.assert_called_once()
    execute.assert_not_called()
    store.compatibility_issues.assert_not_called()
    store.update_simulation_progress.assert_not_called()
    store.finish_simulation_run.assert_not_called()
    assert store.worker_heartbeat.call_count == 2
    last_heartbeat = store.worker_heartbeat.call_args.kwargs
    assert last_heartbeat["ready"] is False
    assert detail in last_heartbeat["detail"]
    assert last_heartbeat["physical_output_connected"] is False
    assert not list(execution.backend.commands_dir.glob("*.json"))
