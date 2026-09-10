from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from fleet_control_plane.isaac_simulation_backend import (
    IsaacSimulationFileBackend, IsaacWorkerPermitV1,
)
from fleet_control_plane.orchestration_models import WorkflowDefinitionV2
from fleet_control_plane.orchestration_store import OrchestrationStore, payload_digest
from fleet_control_plane.store import FleetStore
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBridgeError, UnityRoleBindingV1, UnityRunAborted, UnityScenarioFileBackend,
    _write_json_atomic, load_scenario_configuration,
)
from fleet_control_plane.workflow_authoring import (
    IsaacAdapterProfileV3, UnityAdapterProfileV1, UnityAdapterProfileV2,
    WorkflowAuthoringStore, WorkflowDraftCreateV1, WorkflowDraftReplaceV1,
    WorkflowPublishV1, WorkflowSimulationQueueV1, load_unity_adapter_profile,
)
from fleet_control_plane.workflow_simulation_worker import WorkflowSimulationWorker
import fleet_control_plane.workflow_simulation_worker as worker_module


WORKSPACE = Path(__file__).resolve().parents[3]
PROFILE_PATH = WORKSPACE / "contracts/tasks/isaac-rb5-twin-adapter.v1.json"
WORKFLOW_PATH = WORKSPACE / "contracts/tasks/isaac-rb5-twin-workflow.v1.json"
SCENE_SHA256 = "5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce"
SCENARIO1_PROFILE = WORKSPACE / "contracts/tasks/isaac-scenario1-adapter.v1.json"
SCENARIO1_WORKFLOW = WORKSPACE / "contracts/tasks/isaac-scenario1-workflow.v1.json"
SCENARIO1_CONFIG = WORKSPACE / "config/isaac_scenario1_simulation.yaml"
CANONICAL_SCENARIO1 = json.loads(
    (WORKSPACE / "contracts/tasks/iitp-scenario1-orchestrated.v2.json").read_text()
)
SCENARIO1_BINDINGS = {
    "mobile-carrier": ("isaac-scenario1-carrier", "isaac-scenario1-carrier-agent"),
    "omy-material-handler": ("isaac-scenario1-omy", "isaac-scenario1-omy-agent"),
    "rb5-waam-path": ("isaac-scenario1-rb5", "isaac-scenario1-rb5-agent"),
}


def now():
    return datetime.now(timezone.utc)


def profile_data(root):
    raw = json.loads(PROFILE_PATH.read_text())
    raw["bridge_root"] = str(root)
    return raw


def identity(root, **updates):
    payload = dict(
        schema_version=1, runtime_kind="isaac-sim-executor", profile_id="rb5",
        scene_sha256=SCENE_SHA256, session_id="isaac-test-session",
        updated_at=now().isoformat(), physical_output_connected=False,
    )
    payload.update(updates)
    _write_json_atomic(root / "runtime-identity.json", payload)


def heartbeat(root):
    _write_json_atomic(root / "heartbeat.json", dict(
        schema_version=1, scenario_id="isaac-rb5-twin-pilot", updated_at=now().isoformat(),
        ready=True, observed_phase="READY", lifecycle_state="READY",
        physical_output_connected=False,
    ))


def backend_for(root):
    return IsaacSimulationFileBackend(
        scenario_id="isaac-rb5-twin-pilot", bridge_root=root,
        allowed_steps={"move_offset": "simulate-bounded-joint@1", "return_ready": "simulate-bounded-joint@1"},
        heartbeat_max_age_s=3, result_timeout_s=2, poll_interval_s=0.01,
        profile_id="rb5", scene_sha256=SCENE_SHA256, profile_sha256="a" * 64,
        asset_id="isaac-twin-rb5", agent_id="isaac-twin-rb5-agent", abort_event=Event(),
    )


@pytest.fixture
def bridge():
    with TemporaryDirectory(prefix="sang-isaac-test-", dir="/tmp") as directory:
        root = Path(directory)
        identity(root)
        heartbeat(root)
        yield root


def request(step="move_offset", run_id="isaac-run"):
    return SimpleNamespace(
        execution_mode="SIMULATION", step_id=step, capability_ref="simulate-bounded-joint@1",
        command_id=f"isaac-command-{step}", run_id=run_id, agent_id="isaac-twin-rb5-agent",
        parameters={"operation": "move-offset" if step == "move_offset" else "return-ready",
                    "bound_asset_id": "isaac-twin-rb5"},
        goal=SimpleNamespace(asset_id="isaac-twin-rb5", not_valid_after=now() + timedelta(seconds=20)),
    )


def test_installed_isaac_profile_and_workflow_are_exactly_compatible(tmp_path):
    profile, digest = load_unity_adapter_profile(PROFILE_PATH)
    assert isinstance(profile, IsaacAdapterProfileV3)
    assert not isinstance(profile, UnityAdapterProfileV2)
    assert digest == payload_digest(profile.model_dump(mode="json"))
    assert digest == "2e15d4d9805e42e57d61c059454c04f72e59135ff533e79c748897d4adb6ebdf"
    workflow = WorkflowDefinitionV2.model_validate_json(WORKFLOW_PATH.read_text())
    FleetStore(tmp_path / "test.sqlite3")
    OrchestrationStore(tmp_path / "test.sqlite3")
    store = WorkflowAuthoringStore(tmp_path / "test.sqlite3", adapter_profile_paths=[PROFILE_PATH])
    assert store.compatibility_issues(workflow, profile) == []
    workflow.steps[0].parameters["delta_rad"] = 0.1
    assert "fixed Isaac parameters" in ";".join(store.compatibility_issues(workflow, profile))


@pytest.mark.parametrize("version", [1, 2, 3])
def test_unity_profile_serialization_and_digest_are_unchanged(version):
    path = WORKSPACE / f"contracts/tasks/iitp-scenario1-unity-adapter.v{version}.json"
    raw = json.loads(path.read_text())
    old_model = UnityAdapterProfileV1 if version == 1 else UnityAdapterProfileV2
    expected = old_model.model_validate(raw).model_dump(mode="json")
    profile, digest = load_unity_adapter_profile(path)
    assert profile.model_dump(mode="json") == expected
    assert "execution_runtime" not in expected
    assert digest == payload_digest(expected)
    assert digest == {
        1: "61fdf3a5e0280b4c3b96b2ffc5a24a0923862e3485aa3e3dc7cc2ed7d406d5e6",
        2: "6ec09c69a1f96a7fe4928b10babe988717f157f843af16aa268bd3547de1e178",
        3: "7c779898cd75a711ff2d61e45964eb9b70a04dc1551493290e9105618ee3a75b",
    }[version]


@pytest.mark.parametrize("field,value", [
    ("execution_runtime", "unity"), ("schema_version", 2), ("profile_id", "physical-rb5"),
    ("scene_sha256", "bad"), ("bridge_root", "/tmp/sang-unity-scenario1"),
])
def test_profile_rejects_wrong_identity_and_roots(bridge, field, value):
    raw = profile_data(bridge)
    raw[field] = value
    with pytest.raises(ValueError):
        IsaacAdapterProfileV3.model_validate(raw)


@pytest.mark.parametrize("mutation", ["delta", "third_step", "dependency", "capability"])
def test_profile_rejects_nonfixed_operations(bridge, mutation):
    raw = profile_data(bridge)
    if mutation == "delta":
        raw["steps"][0]["parameters"]["delta_rad"] = 0.1
    elif mutation == "third_step":
        raw["steps"].append({**raw["steps"][1], "step_id": "extra"})
    elif mutation == "dependency":
        raw["steps"][1]["depends_on"] = []
    else:
        raw["steps"][0]["capability"] = "arbitrary@1"
    with pytest.raises(ValueError, match="two fixed"):
        IsaacAdapterProfileV3.model_validate(raw)


@pytest.mark.parametrize("updates", [
    {"runtime_kind": "unity-observer"}, {"profile_id": "omy"},
    {"scene_sha256": "b" * 64}, {"physical_output_connected": True},
    {"physical_output_connected": 0}, {"asset_id": "isaac-twin-rb5"},
    {"updated_at": (now() - timedelta(seconds=60)).isoformat()},
    {"updated_at": (now() + timedelta(seconds=60)).isoformat()},
])
def test_backend_rejects_mismatched_stale_or_nonisolated_identity(bridge, updates):
    identity(bridge, **updates)
    backend = backend_for(bridge)
    with pytest.raises((ValueError, UnityBridgeError)):
        backend.assert_ready()
    assert not (bridge / "commands").exists()


def test_unity_heartbeat_alone_is_not_isaac_identity(bridge):
    (bridge / "runtime-identity.json").unlink()
    with pytest.raises(UnityBridgeError, match="runtime-identity"):
        backend_for(bridge).assert_ready()


def test_session_change_fences_recovery_and_requires_new_worker(bridge):
    backend = backend_for(bridge)
    assert backend.assert_ready().ready
    identity(bridge, session_id="replacement-session")
    with pytest.raises(UnityBridgeError, match="session changed"):
        backend.recover_skill(request())
    assert not (bridge / "commands").exists()


def test_bare_backend_cannot_dispatch_without_worker_fence(bridge):
    with pytest.raises(UnityRunAborted, match="lease fence"):
        backend_for(bridge)._command_for(request(), require_ready=True)
    assert not (bridge / "commands").exists()


def test_resume_cannot_emit_without_worker_fence(bridge):
    with pytest.raises(UnityRunAborted, match="lease fence"):
        backend_for(bridge).send_control(control_id="resume", run_id="isaac-run", action="RESUME")
    assert not (bridge / "controls").exists()


@pytest.mark.parametrize("mutation", ["asset", "agent", "parameters", "physical"])
def test_backend_rejects_unbounded_request(bridge, mutation):
    command = request()
    if mutation == "asset":
        command.goal.asset_id = "physical-rb5"
    elif mutation == "agent":
        command.agent_id = "physical-agent"
    elif mutation == "parameters":
        command.parameters["delta_rad"] = 0.2
    else:
        command.execution_mode = "PHYSICAL"
    with pytest.raises(UnityBridgeError):
        backend_for(bridge)._command_for(command, require_ready=True)
    assert not (bridge / "commands").exists()


def test_permit_is_bounded_to_confirmed_lease_and_cleanup_preserves_other_worker(bridge):
    backend = backend_for(bridge)
    backend.refresh_permit(worker_id="worker-a", run_id="run-a", lease_deadline=time.monotonic() + 0.4)
    permit = IsaacWorkerPermitV1.model_validate_json(backend.permit_path.read_text())
    assert 0 < (permit.not_valid_after - permit.issued_at).total_seconds() <= 0.4
    assert set(permit.model_dump()) == {
        "schema_version", "runtime_kind", "worker_id", "run_id", "profile_id", "scene_sha256",
        "profile_sha256", "session_id", "issued_at", "not_valid_after", "physical_output_connected",
    }
    successor = backend_for(bridge)
    successor.refresh_permit(worker_id="worker-b", run_id="run-b", lease_deadline=time.monotonic() + 10)
    backend.revoke_permit()
    assert json.loads(backend.permit_path.read_text())["worker_id"] == "worker-b"
    successor.revoke_permit()
    assert not backend.permit_path.exists()
    with pytest.raises(UnityRunAborted):
        backend.refresh_permit(worker_id="worker-a", run_id="run-a", lease_deadline=time.monotonic() - 1)


def make_worker(tmp_path, bridge):
    profile_path = tmp_path / "isaac-profile.json"
    _write_json_atomic(profile_path, profile_data(bridge))
    config = dict(
        schema_version=1, scenario_id="isaac-rb5-twin-pilot", workflow_path=str(WORKFLOW_PATH),
        bridge_root=str(bridge), heartbeat_max_age_s=3, result_timeout_s=5,
        roles={"virtual-robot": {"asset_id": "isaac-twin-rb5", "agent_id": "isaac-twin-rb5-agent"}},
    )
    config_path = tmp_path / "config.json"
    _write_json_atomic(config_path, config)
    db = tmp_path / "isolated-test.sqlite3"
    FleetStore(db)
    OrchestrationStore(db)
    store = WorkflowAuthoringStore(db, adapter_profile_paths=[profile_path])
    marker = tmp_path / "unity-marker"
    _write_json_atomic(marker, {"bridge_root": "/tmp/sang-unity-DO-NOT-USE"})
    worker = WorkflowSimulationWorker(
        store=store, worker_id="isaac-test-worker", adapter_profile_path=profile_path,
        scenario_config_path=config_path, runtime_root=tmp_path / "runs", marker_path=marker,
        lease_ttl=timedelta(seconds=10),
    )
    return worker, store


def queue_run(worker, store):
    draft = store.create_draft(
        WorkflowDraftCreateV1(request_id="create", task_definition_id="isaac-pilot", display_name="Isaac pilot"),
        principal_id="test-admin", occurred_at=now(),
    )
    workflow = json.loads(WORKFLOW_PATH.read_text())
    draft = store.update_draft(draft["draft_id"], WorkflowDraftReplaceV1(
        request_id="save", expected_revision=draft["revision"], display_name="Isaac pilot",
        roles=[dict(role="virtual-robot", asset_type="robot", access_mode="exclusive",
                    cardinality="1", requires=["simulate-bounded-joint@1"])],
        steps=workflow["steps"],
    ), principal_id="test-admin", occurred_at=now())
    store.publish(draft["draft_id"], WorkflowPublishV1(request_id="publish", expected_revision=draft["revision"]),
                  principal_id="test-admin", occurred_at=now())
    worker._heartbeat(ready=True, detail="Isaac ready")
    return store.queue_simulation(WorkflowSimulationQueueV1(
        request_id="queue", task_definition_id="isaac-pilot", task_version=1,
        adapter_id=worker.profile.adapter_id, display_name="Isaac pilot simulation",
    ), principal_id="test-admin", occurred_at=now())


def test_worker_selects_isaac_and_ignores_unity_marker(tmp_path, bridge):
    worker, store = make_worker(tmp_path, bridge)
    backend = worker._backend()
    assert isinstance(backend, IsaacSimulationFileBackend)
    assert backend.bridge_root == bridge
    assert worker.run_once() is False
    status = store.list_adapter_status(occurred_at=now())[0]
    assert status["ready"] is True
    assert status["execution_runtime"] == "isaac"
    assert "Unity" not in status["detail"]


def test_worker_lease_loss_revokes_permit_and_blocks_new_dispatch(tmp_path, bridge, monkeypatch):
    worker, store = make_worker(tmp_path, bridge)
    queued = queue_run(worker, store)

    def lose_claim(*, backend, run_id, **kwargs):
        backend._command_for(request(run_id=run_id), require_ready=True)
        assert json.loads(backend.permit_path.read_text())["run_id"] == run_id
        store.worker_heartbeat(
            worker_id="replacement-worker", adapter_id=worker.profile.adapter_id,
            profile_sha256=worker.profile_sha256, scenario_id=worker.profile.scenario_id,
            ready=False, detail="Replacement test Worker", physical_output_connected=False,
            occurred_at=now(),
        )
        with store._connect() as connection:
            connection.execute("UPDATE workflow_simulation_runs SET claimed_worker_id = ? WHERE run_id = ?",
                               ("replacement-worker", run_id))
        with pytest.raises(UnityRunAborted, match="live lease"):
            backend._command_for(request("return_ready", run_id), require_ready=True)
        return {}

    monkeypatch.setattr(worker_module, "execute_scenario", lose_claim)
    assert worker.run_once() is True
    assert len(list((bridge / "commands").glob("*.json"))) == 1
    assert not (bridge / "worker-permit.json").exists()
    result = store.get_simulation_run(queued["run_id"], occurred_at=now())
    assert result["state"] == "RUNNING", result["detail"]


def test_keepalive_refreshes_independently_of_slow_control_poll(tmp_path, bridge, monkeypatch):
    worker, store = make_worker(tmp_path, bridge)
    queue_run(worker, store)
    permits = []
    poll_blocked = Event()

    def slow_control(backend, *, run_id=None, **kwargs):
        if run_id is not None:
            poll_blocked.set()
            time.sleep(1.3)
        return False

    def execute(*, backend, run_id, **kwargs):
        backend._command_for(request(run_id=run_id), require_ready=True)
        assert poll_blocked.wait(1)
        for _ in range(12):
            permits.append(json.loads(backend.permit_path.read_text()))
            time.sleep(0.1)
        return dict(run={"state": "COMPLETE"}, physical_output_connected=False, completed_at=now().isoformat())

    monkeypatch.setattr(worker, "_process_control", slow_control)
    monkeypatch.setattr(worker_module, "execute_scenario", execute)
    assert worker.run_once()
    assert len({permit["issued_at"] for permit in permits}) >= 3
    assert not (bridge / "worker-permit.json").exists()


def test_real_cell_orchestrator_and_isaac_engine_complete_both_steps(tmp_path, bridge):
    from isaac_twin.executor import PilotExecutor

    worker, store = make_worker(tmp_path, bridge)
    queued = queue_run(worker, store)
    engine = PilotExecutor(bridge, worker.profile_sha256)
    stopped = Event()
    errors = []

    def virtual_plant():
        measured, sim_time = [0.0] * 6, 0.0
        try:
            while not stopped.is_set():
                identity(bridge, session_id=engine.session_id)
                _write_json_atomic(bridge / "heartbeat.json", engine.heartbeat())
                measured = engine.tick(measured, sim_time)
                sim_time += 0.1
                time.sleep(0.01)
        except Exception as exc:
            errors.append(exc)

    identity(bridge, session_id=engine.session_id)
    plant = Thread(target=virtual_plant, daemon=True)
    plant.start()
    try:
        assert worker.run_once() is True
    finally:
        stopped.set()
        plant.join(timeout=2)
    assert errors == []
    run = store.get_simulation_run(queued["run_id"], occurred_at=now())
    assert run["state"] == "COMPLETE", run["detail"]
    assert run["result"]["execution_runtime"] == "isaac"
    assert "Unity" not in run["detail"]
    assert run["progress"]["isaac_phase"] == "COMPLETE"
    assert [item["state"] for item in run["progress"]["steps"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert [row["payload"]["step_id"] for row in engine.journal["commands"].values()] == ["move_offset", "return_ready"]
    assert all(row["payload"]["schema_version"] == 1 for row in engine.journal["commands"].values())
    assert not (bridge / "worker-permit.json").exists()


def test_worker_disappearance_expires_runtime_motion_without_resume(bridge):
    from isaac_twin.executor import PilotExecutor

    backend = backend_for(bridge)
    engine = PilotExecutor(bridge, backend.profile_sha256)
    identity(bridge, session_id=engine.session_id)

    @contextmanager
    def fence():
        backend.refresh_permit(worker_id="worker", run_id="isaac-run", lease_deadline=time.monotonic() + 10)
        yield

    backend.command_fence = fence
    backend._command_for(request(), require_ready=True)
    wall, mono = time.time(), time.monotonic()
    measured = [0.0] * 6
    engine.tick(measured, 0, now=wall, mono=mono)
    assert engine.state == "RUNNING"
    held = [0.04, 0, 0, 0, 0, 0]
    assert engine.tick(held, 1, now=wall + 2.1, mono=mono + 2.1) == held
    assert engine.state == "ABORTED"
    backend.refresh_permit(worker_id="worker", run_id="isaac-run", lease_deadline=time.monotonic() + 10)
    assert engine.tick(held, 2) == held
    assert engine.state == "ABORTED"


def scenario1_identity(root, **updates):
    payload = dict(profile_id="scenario1", scene_sha256=json.loads(SCENARIO1_PROFILE.read_text())["scene_sha256"])
    payload.update(updates)
    identity(root, **payload)
    _write_json_atomic(root / "heartbeat.json", dict(
        schema_version=1, scenario_id="isaac-scenario1", updated_at=now().isoformat(),
        ready=True, observed_phase="READY", lifecycle_state="READY", physical_output_connected=False,
    ))


def scenario1_backend(root, **updates):
    profile, digest = load_unity_adapter_profile(SCENARIO1_PROFILE)
    options = dict(
        scenario_id="isaac-scenario1", bridge_root=root, profile_id="scenario1",
        scene_sha256=profile.scene_sha256, profile_sha256=digest,
        allowed_steps={step["step_id"]: step["capability"] for step in CANONICAL_SCENARIO1["steps"]},
        bindings={role: UnityRoleBindingV1(asset_id=asset, agent_id=agent)
                  for role, (asset, agent) in SCENARIO1_BINDINGS.items()},
        heartbeat_max_age_s=3, result_timeout_s=420, poll_interval_s=0.001, abort_event=Event(),
    )
    options.update(updates)
    return IsaacSimulationFileBackend(**options)


def scenario1_request(index=0, run_id="isaac-scenario1-run"):
    step = CANONICAL_SCENARIO1["steps"][index]
    asset, agent = SCENARIO1_BINDINGS[step["role"]]
    return SimpleNamespace(
        execution_mode="SIMULATION", step_id=step["step_id"], capability_ref=step["capability"],
        command_id=f"scenario1-command-{index}", run_id=run_id, agent_id=agent,
        parameters={**step["parameters"], "bound_asset_id": asset},
        goal=SimpleNamespace(asset_id=asset, not_valid_after=now() + timedelta(seconds=420)),
    )


def install_scenario1_fence(backend):
    @contextmanager
    def fence():
        backend.refresh_permit(worker_id="scenario1-worker", run_id="isaac-scenario1-run",
                               lease_deadline=time.monotonic() + 10)
        yield

    backend.command_fence = fence


def test_scenario1_contracts_match_original_five_steps_and_exact_bindings():
    profile, digest = load_unity_adapter_profile(SCENARIO1_PROFILE)
    workflow = json.loads(SCENARIO1_WORKFLOW.read_text())
    config = load_scenario_configuration(SCENARIO1_CONFIG)
    assert profile.profile_id == "scenario1"
    assert profile.adapter_id == "isaac-scenario1-v1"
    assert profile.scenario_id == config.scenario_id == "isaac-scenario1"
    assert profile.bridge_root == config.bridge_root == "/tmp/sang-isaac-scenario1"
    assert config.result_timeout_s == 420
    assert config.workflow_path == "contracts/tasks/isaac-scenario1-workflow.v1.json"
    assert workflow["roles"] == CANONICAL_SCENARIO1["roles"]
    assert workflow["steps"] == CANONICAL_SCENARIO1["steps"]
    assert len(profile.steps) == 5
    assert digest == payload_digest(profile.model_dump(mode="json"))
    assert {role: (binding.asset_id, binding.agent_id) for role, binding in config.roles.items()} == SCENARIO1_BINDINGS


@pytest.mark.parametrize("field,value", [
    ("profile_id", "rb5"), ("profile_id", "arbitrary"), ("schema_version", 1), ("schema_version", 2),
    ("execution_runtime", "unity"), ("adapter_id", "arbitrary"), ("scenario_id", "iitp-scenario1"),
    ("bridge_root", "/tmp/sang-isaac-rb5-twin"), ("scene_sha256", "bad"),
])
def test_scenario1_profile_loader_has_no_arbitrary_profile_bypass(tmp_path, field, value):
    raw = json.loads(SCENARIO1_PROFILE.read_text())
    raw[field] = value
    path = tmp_path / "untrusted-profile.json"
    _write_json_atomic(path, raw)
    with pytest.raises(ValueError):
        load_unity_adapter_profile(path)


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("mutation", ["step_id", "role", "capability", "operation", "input", "dependency"])
def test_scenario1_profile_rejects_edits_to_every_canonical_step(index, mutation):
    raw = json.loads(SCENARIO1_PROFILE.read_text())
    step = raw["steps"][index]
    if mutation == "operation":
        step["parameters"]["operation"] = "arbitrary"
    elif mutation == "input":
        step["parameters"]["speed"] = 0.1
    elif mutation == "dependency":
        step["depends_on"] = ["mobile_return"] if index == 0 else []
    elif mutation == "role":
        step["role"] = next(role for role in SCENARIO1_BINDINGS if role != step["role"])
    else:
        step[mutation] = "arbitrary@1" if mutation == "capability" else "arbitrary"
    with pytest.raises(ValueError):
        IsaacAdapterProfileV3.model_validate(raw)


@pytest.mark.parametrize("mutation", ["role", "access", "requires", "missing_step", "extra_step", "order"])
def test_scenario1_profile_rejects_role_or_sequence_changes(mutation):
    raw = json.loads(SCENARIO1_PROFILE.read_text())
    if mutation in {"role", "access", "requires"}:
        raw["roles"][0][mutation] = ["arbitrary@1"] if mutation == "requires" else "arbitrary"
    elif mutation == "missing_step":
        raw["steps"].pop()
    elif mutation == "extra_step":
        raw["steps"].append({**raw["steps"][0], "step_id": "extra"})
    else:
        raw["steps"].reverse()
    with pytest.raises(ValueError):
        IsaacAdapterProfileV3.model_validate(raw)


@pytest.mark.parametrize("index", range(5))
def test_scenario1_dispatch_and_replay_preserve_exact_v1_wire(bridge, index):
    scenario1_identity(bridge)
    backend = scenario1_backend(bridge)
    install_scenario1_fence(backend)
    request = scenario1_request(index)
    command, _ = backend._command_for(request, require_ready=True)
    encoded = (backend.commands_dir / f"{request.command_id}.json").read_bytes()
    replay, _ = backend._command_for(request, require_ready=False)
    assert replay == command
    assert (backend.commands_dir / f"{request.command_id}.json").read_bytes() == encoded
    assert command.schema_version == 1
    assert command.agent_id == request.agent_id
    assert command.capability == request.capability_ref
    assert command.parameters == request.parameters
    assert command.execution_mode == "SIMULATION"
    assert command.physical_output_connected is False
    permit = IsaacWorkerPermitV1.model_validate_json(backend.permit_path.read_text())
    assert permit.schema_version == 1
    assert permit.runtime_kind == "isaac-sim-executor"
    assert permit.profile_id == "scenario1"
    assert permit.scene_sha256 == backend.scene_sha256
    assert permit.profile_sha256 == backend.profile_sha256
    assert permit.session_id == backend.session_id
    assert 0 < (permit.not_valid_after - permit.issued_at).total_seconds() <= 2
    request.run_id = "different-run"
    with pytest.raises(UnityBridgeError, match="reused"):
        backend._command_for(request, require_ready=False)
    assert (backend.commands_dir / f"{request.command_id}.json").read_bytes() == encoded


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("mutation", ["cross_asset", "bound_asset", "wrong_agent", "wrong_operation", "extra_input", "capability", "physical"])
def test_scenario1_dispatch_rejects_wrong_binding_or_operation_per_step(bridge, index, mutation):
    scenario1_identity(bridge)
    backend = scenario1_backend(bridge)
    request = scenario1_request(index)
    other_asset, other_agent = next(binding for binding in SCENARIO1_BINDINGS.values()
                                    if binding[0] != request.goal.asset_id)
    if mutation == "cross_asset":
        request.goal.asset_id = other_asset
        request.parameters["bound_asset_id"] = other_asset
        request.agent_id = other_agent
    elif mutation == "wrong_agent":
        request.agent_id = other_agent
    elif mutation == "bound_asset":
        request.parameters["bound_asset_id"] = other_asset
    elif mutation == "wrong_operation":
        request.parameters["operation"] = CANONICAL_SCENARIO1["steps"][(index + 1) % 5]["parameters"]["operation"]
    elif mutation == "extra_input":
        request.parameters["delta_rad"] = 0.1
    elif mutation == "capability":
        request.capability_ref = "arbitrary@1"
    else:
        request.execution_mode = "PHYSICAL"
    with pytest.raises(UnityBridgeError):
        backend._command_for(request, require_ready=True)
    assert not backend.commands_dir.exists()
    assert not backend.permit_path.exists()


@pytest.mark.parametrize("updates", [
    {"schema_version": 2}, {"runtime_kind": "unity-observer"}, {"profile_id": "rb5"},
    {"scene_sha256": "b" * 64}, {"physical_output_connected": True}, {"physical_output_connected": 0},
    {"updated_at": (now() - timedelta(seconds=60)).isoformat()},
])
def test_scenario1_identity_mismatch_blocks_dispatch_control_and_permit(bridge, updates):
    scenario1_identity(bridge, **updates)
    backend = scenario1_backend(bridge)
    for action in (
        lambda: backend._command_for(scenario1_request(), require_ready=False),
        lambda: backend.send_control(control_id="reset", run_id="isaac-scenario1-run", action="RESET"),
        lambda: backend.refresh_permit(worker_id="worker", run_id="run", lease_deadline=time.monotonic() + 10),
    ):
        with pytest.raises((ValueError, UnityBridgeError)):
            action()
    assert not backend.commands_dir.exists()
    assert not backend.controls_dir.exists()
    assert not backend.permit_path.exists()


@pytest.mark.parametrize("mutation", ["role", "asset", "agent", "single_asset", "capability", "scenario", "profile", "timeout"])
def test_scenario1_backend_constructor_cannot_remap_fixed_assets(bridge, mutation):
    options = {"bindings": {role: UnityRoleBindingV1(asset_id=asset, agent_id=agent)
                            for role, (asset, agent) in SCENARIO1_BINDINGS.items()}}
    if mutation == "role":
        options["bindings"]["arbitrary"] = options["bindings"].pop("mobile-carrier")
    elif mutation in {"asset", "agent"}:
        setattr(options["bindings"]["mobile-carrier"], f"{mutation}_id", "arbitrary")
    elif mutation == "single_asset":
        options.update(asset_id="isaac-scenario1-carrier", agent_id="isaac-scenario1-carrier-agent")
    elif mutation == "capability":
        options["allowed_steps"] = {"mobile_delivery": "arbitrary@1"}
    elif mutation == "scenario":
        options["scenario_id"] = "arbitrary"
    elif mutation == "profile":
        options["profile_id"] = "arbitrary"
    else:
        options["result_timeout_s"] = 421
    with pytest.raises(ValueError):
        scenario1_backend(bridge, **options)


def test_scenario1_backend_copies_validated_bindings(bridge):
    scenario1_identity(bridge)
    bindings = {role: UnityRoleBindingV1(asset_id=asset, agent_id=agent)
                for role, (asset, agent) in SCENARIO1_BINDINGS.items()}
    backend = scenario1_backend(bridge, bindings=bindings)
    bindings["mobile-carrier"].asset_id = "changed-after-validation"
    assert backend.bindings["mobile-carrier"].asset_id == "isaac-scenario1-carrier"
    with pytest.raises(UnityRunAborted, match="lease fence"):
        backend._command_for(scenario1_request(), require_ready=True)
    assert not backend.commands_dir.exists()


def control_result(control_action, **updates):
    result = dict(
        schema_version=1, scenario_id="isaac-scenario1", control_id="control", run_id="isaac-scenario1-run",
        action=control_action, state="ACKNOWLEDGED", detail="Test acknowledgement only", observed_phase="READY",
        completed_at=now().isoformat(), physical_output_connected=False, output_isolation_confirmed=True,
    )
    result.update(updates)
    return result


@pytest.mark.parametrize("action", ["HOLD", "RESUME", "ABORT", "RESET"])
def test_scenario1_controls_preserve_v1_replay_and_session_fencing(bridge, action):
    scenario1_identity(bridge)
    backend = scenario1_backend(bridge)
    if action == "RESUME":
        with pytest.raises(UnityRunAborted, match="lease fence"):
            backend.send_control(control_id="control", run_id="isaac-scenario1-run", action=action)
        assert not backend.controls_dir.exists()
        install_scenario1_fence(backend)
    _write_json_atomic(backend.control_results_dir / "control.json", control_result(action))
    for _ in range(2):
        assert backend.send_control(control_id="control", run_id="isaac-scenario1-run", action=action).state == "ACKNOWLEDGED"
    control = json.loads((backend.controls_dir / "control.json").read_text())
    assert control["schema_version"] == 1
    assert control["execution_mode"] == "SIMULATION"
    assert control["physical_output_connected"] is False
    if action != "RESUME":
        assert not backend.permit_path.exists()
    with pytest.raises(UnityBridgeError, match="reused"):
        backend.send_control(control_id="control", run_id="another-run", action=action)
    scenario1_identity(bridge, session_id="after-reset-session")
    with pytest.raises(UnityBridgeError, match="session changed"):
        backend.send_control(control_id="next-control", run_id="next-run", action="RESET")
    with pytest.raises(UnityBridgeError, match="session changed"):
        backend.recover_skill(scenario1_request())
    assert not (backend.controls_dir / "next-control.json").exists()
    assert scenario1_backend(bridge).assert_ready().ready


@pytest.mark.parametrize("updates", [{"schema_version": 2}, {"scenario_id": "other"}, {"action": "ABORT"}, {"physical_output_connected": True}])
def test_scenario1_rejects_wrong_control_result_schema_and_identity(bridge, updates):
    scenario1_identity(bridge)
    backend = scenario1_backend(bridge)
    _write_json_atomic(backend.control_results_dir / "control.json", control_result("RESET", **updates))
    with pytest.raises((ValueError, UnityBridgeError)):
        backend.send_control(control_id="control", run_id="isaac-scenario1-run", action="RESET")


def test_scenario1_rejects_unsupported_control_and_result_schema(bridge):
    scenario1_identity(bridge)
    backend = scenario1_backend(bridge)
    with pytest.raises(ValueError):
        backend.send_control(control_id="control", run_id="isaac-scenario1-run", action="MOVE")
    assert not backend.controls_dir.exists()
    result_path = backend.results_dir / "result.json"
    request = scenario1_request()
    _write_json_atomic(result_path, dict(
        schema_version=2, scenario_id=backend.scenario_id, command_id=request.command_id,
        run_id=request.run_id, step_id=request.step_id, state="SUCCEEDED", detail="Invalid schema",
        observed_phase="COMPLETE", completed_at=now().isoformat(), physical_output_connected=False,
    ))
    with pytest.raises(ValueError):
        backend._wait_for_result(request, result_path)


def make_scenario1_worker(tmp_path, config_updates=None):
    config = load_scenario_configuration(SCENARIO1_CONFIG).model_dump(mode="json")
    config.update(config_updates or {})
    config_path = tmp_path / "scenario1-config.json"
    _write_json_atomic(config_path, config)
    db = tmp_path / "scenario1.sqlite3"
    FleetStore(db)
    OrchestrationStore(db)
    store = WorkflowAuthoringStore(db, adapter_profile_paths=[SCENARIO1_PROFILE])
    marker = tmp_path / "unity-marker.json"
    _write_json_atomic(marker, {"bridge_root": "/tmp/sang-unity-DO-NOT-USE"})
    worker = WorkflowSimulationWorker(
        store=store, worker_id="scenario1-worker", adapter_profile_path=SCENARIO1_PROFILE,
        scenario_config_path=config_path, runtime_root=tmp_path / "runs", marker_path=marker,
        lease_ttl=timedelta(seconds=10),
    )
    return worker, store


def test_scenario1_worker_selects_exact_profile_not_unity_marker(tmp_path):
    worker, _ = make_scenario1_worker(tmp_path)
    backend = worker._backend()
    assert type(backend) is IsaacSimulationFileBackend
    assert backend.bridge_root == Path("/tmp/sang-isaac-scenario1")
    assert backend.result_timeout_s == 420
    assert backend.bindings == worker.config.roles
    assert backend.profile_id == "scenario1"
    assert backend.profile_sha256 == worker.profile_sha256


@pytest.mark.parametrize("role", SCENARIO1_BINDINGS)
@pytest.mark.parametrize("field", ["asset_id", "agent_id"])
def test_scenario1_worker_rejects_remapping_each_role(tmp_path, role, field):
    roles = load_scenario_configuration(SCENARIO1_CONFIG).model_dump(mode="json")["roles"]
    roles[role][field] = "wrong-binding"
    with pytest.raises(ValueError, match="same virtual assets"):
        make_scenario1_worker(tmp_path, {"roles": roles})


@pytest.mark.parametrize("updates", [
    {"schema_version": 2}, {"scenario_id": "isaac-rb5-twin-pilot"},
    {"bridge_root": "/tmp/sang-isaac-rb5-twin"}, {"result_timeout_s": 421}, {"roles": {}},
])
def test_scenario1_worker_rejects_wrong_config_identity_or_timeout(tmp_path, updates):
    with pytest.raises(ValueError):
        make_scenario1_worker(tmp_path, updates)


@pytest.mark.parametrize("index", range(5))
def test_scenario1_compatibility_has_no_editable_operation_inputs(tmp_path, index):
    worker, store = make_scenario1_worker(tmp_path)
    workflow = WorkflowDefinitionV2.model_validate_json(SCENARIO1_WORKFLOW.read_text())
    assert store.compatibility_issues(workflow, worker.profile) == []
    workflow.steps[index].parameters["speed"] = 0.1
    assert "fixed Isaac parameters" in ";".join(store.compatibility_issues(workflow, worker.profile))


def test_scenario1_publication_and_worker_dispatch_all_five_steps(tmp_path, bridge, monkeypatch):
    worker, store = make_scenario1_worker(tmp_path)
    scenario1_identity(bridge)
    draft = store.create_draft(WorkflowDraftCreateV1(
        request_id="create-scenario1", task_definition_id="isaac-scenario1", display_name="Isaac Scenario 1",
    ), principal_id="test-admin", occurred_at=now())
    workflow = json.loads(SCENARIO1_WORKFLOW.read_text())
    draft = store.update_draft(draft["draft_id"], WorkflowDraftReplaceV1(
        request_id="save-scenario1", expected_revision=draft["revision"], display_name="Isaac Scenario 1",
        roles=[dict(role=role["role"], asset_type="robot", access_mode=role["access"],
                    cardinality=role["cardinality"], requires=role["selector"]["requires"])
               for role in workflow["roles"]], steps=workflow["steps"],
    ), principal_id="test-admin", occurred_at=now())
    store.publish(draft["draft_id"], WorkflowPublishV1(
        request_id="publish-scenario1", expected_revision=draft["revision"],
    ), principal_id="test-admin", occurred_at=now())
    worker._heartbeat(ready=True, detail="Scenario 1 test responder ready")
    queued = store.queue_simulation(WorkflowSimulationQueueV1(
        request_id="queue-scenario1", task_definition_id="isaac-scenario1", task_version=1,
        adapter_id=worker.profile.adapter_id, display_name="Scenario 1 test run",
    ), principal_id="test-admin", occurred_at=now())
    original_backend = worker._backend
    dispatched = []

    def isolated_backend(**kwargs):
        # Validate the installed profile normally; redirect only file I/O in this test.
        installed = original_backend(**kwargs)
        backend = scenario1_backend(bridge, abort_event=installed.abort_event)
        original_wait = backend._wait_for_result

        def respond(request, result_path):
            dispatched.append(request)
            _write_json_atomic(result_path, dict(
                schema_version=1, scenario_id=backend.scenario_id, command_id=request.command_id,
                run_id=request.run_id, step_id=request.step_id, state="SUCCEEDED",
                detail="Simulated file response, not motion evidence", observed_phase="COMPLETE",
                completed_at=now().isoformat(), physical_output_connected=False,
            ))
            return original_wait(request, result_path)

        backend._wait_for_result = respond
        return backend

    monkeypatch.setattr(worker, "_backend", isolated_backend)
    assert worker.run_once()
    result = store.get_simulation_run(queued["run_id"], occurred_at=now())
    assert result["state"] == "COMPLETE", result["detail"]
    assert [step["state"] for step in result["progress"]["steps"]] == ["SUCCEEDED"] * 5
    assert result["result"]["profile_id"] == "scenario1"
    assert result["result"]["execution_runtime"] == "isaac"
    assert result["result"]["scene_sha256"] == worker.profile.scene_sha256
    assert result["result"]["physical_output_connected"] is False
    assert [request.step_id for request in dispatched] == [step["step_id"] for step in workflow["steps"]]
    for request, step in zip(dispatched, workflow["steps"], strict=True):
        asset, agent = SCENARIO1_BINDINGS[step["role"]]
        assert request.agent_id == agent
        assert request.goal.asset_id == asset
        assert request.capability_ref == step["capability"]
        assert request.parameters == {**step["parameters"], "bound_asset_id": asset}
        wire = json.loads((bridge / "commands" / f"{request.command_id}.json").read_text())
        assert wire["schema_version"] == 1
    assert not (bridge / "worker-permit.json").exists()
