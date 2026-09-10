from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from fleet_control_plane.orchestration_models import WorkflowDefinitionV2
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBridgeError,
    UnityRunAborted,
    UnityScenarioFileBackend,
    _write_json_atomic,
    allowed_step_capabilities,
    execute_scenario,
    load_scenario_configuration,
    load_workflow,
)


NOW = datetime.now(timezone.utc)


def _paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[3]
    return (
        root,
        root / "config/unity_scenario1_simulation.yaml",
        root / "contracts/tasks/iitp-scenario1-unity-adapter.v1.json",
    )


def _heartbeat(
    root: Path, *, updated_at: datetime | None = None, ready: bool = True
) -> None:
    observed_at = datetime.now(timezone.utc) if updated_at is None else updated_at
    _write_json_atomic(
        root / "heartbeat.json",
        {
            "schema_version": 1,
            "scenario_id": "iitp-scenario1",
            "updated_at": observed_at.isoformat(),
            "ready": ready,
            "active_command_id": None,
            "expected_step": "mobile_delivery",
            "observed_step": "mobile_delivery",
            "observed_phase": "ReadyDelivery",
            "physical_output_connected": False,
        },
    )


def _request(step_id: str, capability: str):
    return SimpleNamespace(
        execution_mode="SIMULATION",
        step_id=step_id,
        capability_ref=capability,
        command_id="11111111-1111-4111-8111-111111111111",
        run_id="test-run",
        agent_id="unity-mobile-agent",
        parameters={"operation": "fixture"},
        goal=SimpleNamespace(not_valid_after=NOW + timedelta(minutes=5)),
    )


def test_workflow_and_unity_profile_have_the_same_allowlist():
    _, config_path, unity_profile_path = _paths()
    config = load_scenario_configuration(config_path)
    workflow = load_workflow(config_path, config)
    assert isinstance(workflow, WorkflowDefinitionV2)
    assert [step.step_id for step in workflow.steps] == [
        "mobile_delivery",
        "omy_pre_pick_place",
        "rb5_waam",
        "omy_post_pick_place",
        "mobile_return",
    ]
    unity_profile = json.loads(unity_profile_path.read_text(encoding="utf-8"))
    assert allowed_step_capabilities(workflow) == {
        item["step_id"]: item["capability"] for item in unity_profile["steps"]
    }


def test_bridge_round_trip_and_result_identity(tmp_path):
    _heartbeat(tmp_path)
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=5,
        result_timeout_s=2,
        poll_interval_s=0.01,
    )
    request = _request("mobile_delivery", "simulate-material-delivery@1")

    def respond() -> None:
        command_path = tmp_path / "commands" / f"{request.command_id}.json"
        for _ in range(200):
            if command_path.exists():
                command = json.loads(command_path.read_text(encoding="utf-8"))
                _write_json_atomic(
                    tmp_path / "results" / f"{request.command_id}.json",
                    {
                        "schema_version": 1,
                        "scenario_id": command["scenario_id"],
                        "command_id": command["command_id"],
                        "run_id": command["run_id"],
                        "step_id": command["step_id"],
                        "state": "SUCCEEDED",
                        "detail": "fixture completed",
                        "observed_phase": "ReadyPrePickPlace",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "physical_output_connected": False,
                        "output_isolation_confirmed": True,
                    },
                )
                return
            time.sleep(0.01)

    responder = Thread(target=respond, daemon=True)
    responder.start()
    result = backend.execute_skill(request)
    responder.join(timeout=1)
    assert result["state"] == "SUCCEEDED"
    assert result["physical_output_connected"] is False


def test_bridge_result_wait_stops_immediately_after_abort(tmp_path):
    _heartbeat(tmp_path)
    aborted = Event()
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=5,
        result_timeout_s=2,
        poll_interval_s=0.01,
        abort_event=aborted,
    )
    request = _request("mobile_delivery", "simulate-material-delivery@1")

    def acknowledge_abort() -> None:
        command_path = tmp_path / "commands" / f"{request.command_id}.json"
        for _ in range(200):
            if command_path.exists():
                aborted.set()
                return
            time.sleep(0.01)

    responder = Thread(target=acknowledge_abort, daemon=True)
    responder.start()
    started = time.monotonic()
    with pytest.raises(UnityRunAborted, match="aborted while waiting"):
        backend.execute_skill(request)
    responder.join(timeout=1)
    assert time.monotonic() - started < 0.5


def test_bridge_waits_for_transient_unity_readiness(tmp_path):
    _heartbeat(tmp_path, ready=False)
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=5,
        result_timeout_s=2,
        poll_interval_s=0.01,
    )
    request = _request("mobile_delivery", "simulate-material-delivery@1")

    def become_ready_and_respond() -> None:
        time.sleep(0.1)
        _heartbeat(tmp_path, ready=True)
        command_path = tmp_path / "commands" / f"{request.command_id}.json"
        for _ in range(200):
            if command_path.exists():
                command = json.loads(command_path.read_text(encoding="utf-8"))
                _write_json_atomic(
                    tmp_path / "results" / f"{request.command_id}.json",
                    {
                        "schema_version": 1,
                        "scenario_id": command["scenario_id"],
                        "command_id": command["command_id"],
                        "run_id": command["run_id"],
                        "step_id": command["step_id"],
                        "state": "SUCCEEDED",
                        "detail": "fixture completed after settling",
                        "observed_phase": "ReadyPrePickPlace",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "physical_output_connected": False,
                        "output_isolation_confirmed": True,
                    },
                )
                return
            time.sleep(0.01)

    responder = Thread(target=become_ready_and_respond, daemon=True)
    responder.start()
    result = backend.execute_skill(request)
    responder.join(timeout=1)
    assert result["state"] == "SUCCEEDED"


def test_bridge_rejects_stale_or_unallowlisted_commands(tmp_path):
    _heartbeat(tmp_path, updated_at=NOW - timedelta(minutes=1))
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=1,
        result_timeout_s=1,
    )
    with pytest.raises(UnityBridgeError, match="stale"):
        backend.assert_ready()

    _heartbeat(tmp_path, updated_at=datetime.now(timezone.utc))
    with pytest.raises(UnityBridgeError, match="allowlisted"):
        backend.execute_skill(_request("mobile_delivery", "wrong-capability@1"))
    assert not (tmp_path / "commands").exists()


def test_bridge_control_round_trip_is_output_isolated(tmp_path):
    _heartbeat(tmp_path)
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=5,
        result_timeout_s=2,
        poll_interval_s=0.01,
    )

    def respond() -> None:
        path = tmp_path / "controls" / "simctl-hold-test.json"
        for _ in range(200):
            if path.exists():
                control = json.loads(path.read_text(encoding="utf-8"))
                _write_json_atomic(
                    tmp_path / "control-results" / "simctl-hold-test.json",
                    {
                        "schema_version": 1,
                        "scenario_id": "iitp-scenario1",
                        "control_id": "simctl-hold-test",
                        "run_id": "test-run",
                        "action": "HOLD",
                        "state": "ACKNOWLEDGED",
                        "detail": "Simulation held",
                        "observed_phase": "Delivering",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "physical_output_connected": False,
                        "output_isolation_confirmed": True,
                    },
                )
                assert control["physical_output_connected"] is False
                return
            time.sleep(0.01)

    responder = Thread(target=respond, daemon=True)
    responder.start()
    result = backend.send_control(
        control_id="simctl-hold-test",
        run_id="test-run",
        action="HOLD",
        timeout_s=2,
    )
    responder.join(timeout=1)
    assert result.state == "ACKNOWLEDGED"
    assert result.physical_output_connected is False


def test_bridge_recovery_reuses_the_exact_existing_command(tmp_path):
    _heartbeat(tmp_path)
    backend = UnityScenarioFileBackend(
        scenario_id="iitp-scenario1",
        bridge_root=tmp_path,
        allowed_steps={"mobile_delivery": "simulate-material-delivery@1"},
        heartbeat_max_age_s=5,
        result_timeout_s=2,
        poll_interval_s=0.01,
    )
    request = _request("mobile_delivery", "simulate-material-delivery@1")
    _, result_path = backend._command_for(request, require_ready=True)
    command_path = tmp_path / "commands" / f"{request.command_id}.json"
    original = command_path.read_bytes()
    _write_json_atomic(
        result_path,
        {
            "schema_version": 1,
            "scenario_id": "iitp-scenario1",
            "command_id": request.command_id,
            "run_id": request.run_id,
            "step_id": request.step_id,
            "state": "SUCCEEDED",
            "detail": "Recovered existing Unity result",
            "observed_phase": "ReadyPrePickPlace",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "physical_output_connected": False,
            "output_isolation_confirmed": True,
        },
    )

    recovered = backend.recover_skill(request)
    assert recovered["state"] == "SUCCEEDED"
    assert command_path.read_bytes() == original
    assert len(list((tmp_path / "commands").glob("*.json"))) == 1


class _ImmediateBackend:
    command_sink_present = False
    process_output_connected = False

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.parameters: list[dict] = []

    def execute_skill(self, request) -> dict:
        self.steps.append(request.step_id)
        self.parameters.append(dict(request.parameters))
        return {
            "scenario_id": "iitp-scenario1",
            "command_id": request.command_id,
            "run_id": request.run_id,
            "step_id": request.step_id,
            "state": "SUCCEEDED",
            "detail": "fixture completed",
            "observed_phase": request.step_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "physical_output_connected": False,
            "output_isolation_confirmed": True,
        }


def test_full_scenario_uses_cell_orchestrator_in_dependency_order(tmp_path):
    _, config_path, _ = _paths()
    config = load_scenario_configuration(config_path, bridge_root=tmp_path / "bridge")
    workflow = load_workflow(config_path, config)
    backend = _ImmediateBackend()
    summary = execute_scenario(
        config=config,
        workflow=workflow,
        backend=backend,
        runtime_dir=tmp_path / "runtime",
        run_id="scenario-fixture-run",
        now=NOW,
    )
    assert summary["run"]["state"] == "COMPLETE"
    assert [step["state"] for step in summary["run"]["steps"]] == ["SUCCEEDED"] * 5
    assert backend.steps == [step.step_id for step in workflow.steps]
    assert all(item["bound_asset_id"] for item in backend.parameters)
    assert summary["physical_output_connected"] is False


def test_full_scenario_fixture_repeats_ten_independent_runs(tmp_path):
    _, config_path, _ = _paths()
    config = load_scenario_configuration(config_path, bridge_root=tmp_path / "bridge")
    workflow = load_workflow(config_path, config)
    expected_steps = [step.step_id for step in workflow.steps]

    for index in range(10):
        backend = _ImmediateBackend()
        summary = execute_scenario(
            config=config,
            workflow=workflow,
            backend=backend,
            runtime_dir=tmp_path / f"runtime-{index}",
            run_id=f"scenario-fixture-repeat-{index}",
            now=NOW + timedelta(seconds=index),
        )
        assert summary["run"]["state"] == "COMPLETE"
        assert backend.steps == expected_steps
        assert summary["physical_output_connected"] is False


def test_execute_scenario_continues_after_reconciliation_returns_active(
    tmp_path, monkeypatch
):
    _, config_path, _ = _paths()
    config = load_scenario_configuration(config_path, bridge_root=tmp_path / "bridge")
    workflow = load_workflow(config_path, config)
    states = iter(["ACTIVE", "COMPLETE"])
    calls = []

    def tick(_orchestrator, run_id, *, now):
        calls.append((run_id, now))
        return {
            "run_id": run_id,
            "state": next(states),
            "steps": [],
            "commands": [],
        }

    monkeypatch.setattr(
        "fleet_control_plane.unity_scenario_orchestrator.CellOrchestrator.tick",
        tick,
    )
    summary = execute_scenario(
        config=config,
        workflow=workflow,
        backend=_ImmediateBackend(),
        runtime_dir=tmp_path / "runtime",
        run_id="scenario-reconciliation-run",
        now=NOW,
    )
    assert summary["run"]["state"] == "COMPLETE"
    assert [item[0] for item in calls] == [
        "scenario-reconciliation-run",
        "scenario-reconciliation-run",
    ]
