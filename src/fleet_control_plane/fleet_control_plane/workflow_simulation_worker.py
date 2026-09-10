"""Durable local Worker for output-disconnected Workflow simulation Runs."""

from __future__ import annotations

from contextlib import contextmanager
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
import time

from fleet_control_plane.orchestration_models import WorkflowDefinitionV2
from fleet_control_plane.isaac_simulation_backend import (
    ISAAC_SCENARIO1_BINDINGS,
    IsaacSimulationFileBackend,
    PERMIT_REFRESH_SECONDS,
)
from fleet_control_plane.status import parse_datetime
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityRoleBindingV1,
    UnityRunAborted,
    UnityScenarioFileBackend,
    execute_scenario,
    load_scenario_configuration,
)
from fleet_control_plane.unity_dynamic_binding import resolve_unity_workflow
from fleet_control_plane.workflow_authoring import IsaacAdapterProfileV3, UnityAdapterProfileV2
from fleet_control_plane.workflow_authoring import (
    WorkflowAuthoringConflict,
    WorkflowAuthoringStore,
    WorkflowAuthoringUnavailable,
    load_unity_adapter_profile,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bridge_root(default_root: str, marker_path: Path) -> Path:
    candidate = default_root
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            candidate = marker.get("bridge_root", candidate)
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise ValueError("Unity orchestrator marker is invalid") from exc
    root = Path(candidate).expanduser().resolve()
    if not root.is_absolute() or not str(root).startswith("/tmp/sang-unity-"):
        raise ValueError("Unity bridge root must remain below /tmp/sang-unity-*")
    return root


def _read_progress(runtime_dir: Path, fallback: dict) -> dict:
    database = runtime_dir / "orchestrator.sqlite3"
    if not database.exists():
        return fallback
    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            steps = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT step_id, state, updated_at
                    FROM cell_orchestrator_steps ORDER BY rowid
                    """
                ).fetchall()
            ]
            run = connection.execute(
                """
                SELECT state, reason, updated_at
                FROM cell_orchestrator_runs LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return fallback
    return {
        "steps": steps,
        "cell_state": None if run is None else run["state"],
        "cell_reason": None if run is None else run["reason"],
        "cell_updated_at": None if run is None else run["updated_at"],
    }


class WorkflowSimulationWorker:
    @property
    def runtime_name(self) -> str:
        return "Isaac" if isinstance(self.profile, IsaacAdapterProfileV3) else "Unity"

    def __init__(
        self,
        *,
        store: WorkflowAuthoringStore,
        worker_id: str,
        adapter_profile_path: Path,
        scenario_config_path: Path,
        runtime_root: Path,
        marker_path: Path,
        lease_ttl: timedelta,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.profile, self.profile_sha256 = load_unity_adapter_profile(
            adapter_profile_path
        )
        self.config = load_scenario_configuration(scenario_config_path)
        self.runtime_root = runtime_root.expanduser().resolve()
        self.marker_path = marker_path
        self.lease_ttl = lease_ttl
        if self.config.scenario_id != self.profile.scenario_id:
            raise ValueError(f"{self.runtime_name} scenario config and adapter profile disagree")
        if (not isinstance(self.profile, UnityAdapterProfileV2)
                and set(self.config.roles) != {role.role for role in self.profile.roles}):
            raise ValueError(f"{self.runtime_name} role bindings do not match the adapter profile")
        if isinstance(self.profile, IsaacAdapterProfileV3):
            expected_bindings = ISAAC_SCENARIO1_BINDINGS if self.profile.profile_id == "scenario1" else {
                "virtual-robot": (f"isaac-twin-{self.profile.profile_id}",
                                  f"isaac-twin-{self.profile.profile_id}-agent"),
            }
            if ({role: (binding.asset_id, binding.agent_id) for role, binding in self.config.roles.items()}
                    != expected_bindings
                    or Path(self.config.bridge_root).resolve() != Path(self.profile.bridge_root).resolve()):
                raise ValueError("Isaac config must pin the same virtual assets, agents and bridge root")
            if self.profile.profile_id == "scenario1" and self.config.result_timeout_s > 420:
                raise ValueError("Isaac Scenario 1 result timeout must be within 420 seconds")
        configured = self.store.adapter_profiles.get(self.profile.adapter_id)
        if configured is None or configured[1] != self.profile_sha256:
            raise ValueError("Central store is not configured with this adapter digest")

    def _backend(self, *, abort_event: Event | None = None) -> UnityScenarioFileBackend:
        if isinstance(self.profile, IsaacAdapterProfileV3):
            if self.profile.profile_id == "scenario1":
                binding_options = {"bindings": self.config.roles}
            else:
                binding = self.config.roles["virtual-robot"]
                binding_options = {"asset_id": binding.asset_id, "agent_id": binding.agent_id}
            return IsaacSimulationFileBackend(
                scenario_id=self.profile.scenario_id,
                bridge_root=self.profile.bridge_root,
                allowed_steps={step.step_id: step.capability for step in self.profile.steps},
                heartbeat_max_age_s=self.config.heartbeat_max_age_s,
                result_timeout_s=self.config.result_timeout_s,
                poll_interval_s=self.profile.poll_interval_seconds,
                abort_event=abort_event,
                profile_id=self.profile.profile_id,
                scene_sha256=self.profile.scene_sha256,
                profile_sha256=self.profile_sha256,
                **binding_options,
            )
        root = _bridge_root(self.profile.bridge_root, self.marker_path)
        return UnityScenarioFileBackend(
            scenario_id=self.profile.scenario_id,
            bridge_root=root,
            allowed_steps={step.step_id: step.capability for step in self.profile.steps},
            heartbeat_max_age_s=self.config.heartbeat_max_age_s,
            result_timeout_s=self.config.result_timeout_s,
            poll_interval_s=self.profile.poll_interval_seconds,
            abort_event=abort_event,
            dynamic_binding=(self.profile.dynamic_binding
                             if isinstance(self.profile, UnityAdapterProfileV2) else None),
        )

    def _heartbeat(self, *, ready: bool, detail: str) -> None:
        self.store.worker_heartbeat(
            worker_id=self.worker_id,
            adapter_id=self.profile.adapter_id,
            profile_sha256=self.profile_sha256,
            scenario_id=self.profile.scenario_id,
            ready=ready,
            detail=detail,
            physical_output_connected=False,
            occurred_at=_utc_now(),
        )

    def _progress_with_heartbeat(
        self,
        runtime_dir: Path,
        fallback: dict,
        heartbeat,
    ) -> dict:
        progress = _read_progress(runtime_dir, fallback)
        prefix = self.runtime_name.lower()
        return {
            **progress,
            "lifecycle_state": heartbeat.lifecycle_state,
            f"{prefix}_phase": heartbeat.observed_phase,
            f"{prefix}_current_run_id": heartbeat.current_run_id,
            f"{prefix}_active_command_id": heartbeat.active_command_id,
            f"{prefix}_updated_at": heartbeat.updated_at.isoformat(),
        }

    def _process_control(
        self,
        backend: UnityScenarioFileBackend,
        *,
        run_id: str | None = None,
        aborted: Event | None = None,
    ) -> bool:
        control = self.store.claim_simulation_control(
            worker_id=self.worker_id,
            adapter_id=self.profile.adapter_id,
            claim_ttl=self.lease_ttl,
            occurred_at=_utc_now(),
            run_id=run_id,
        )
        if control is None:
            return False
        try:
            result = backend.send_control(
                control_id=control["control_id"],
                run_id=control["run_id"],
                action=control["action"],
            )
        except Exception as exc:
            self.store.finish_simulation_control(
                control["control_id"],
                worker_id=self.worker_id,
                state="REJECTED",
                detail=f"{self.runtime_name} rejected {control['action']}: {exc}",
                occurred_at=_utc_now(),
            )
        else:
            self.store.finish_simulation_control(
                control["control_id"],
                worker_id=self.worker_id,
                state="ACKNOWLEDGED",
                detail=result.detail,
                occurred_at=_utc_now(),
            )
            if control["action"] == "ABORT" and aborted is not None:
                aborted.set()
        return True

    def run_once(self) -> bool:
        aborted = Event()
        try:
            backend = self._backend(abort_event=aborted)
            heartbeat = backend.read_heartbeat()
        except Exception as exc:
            self._heartbeat(ready=False, detail=f"{self.runtime_name} unavailable: {exc}")
            return False
        self._heartbeat(
            ready=heartbeat.ready,
            detail=f"{self.runtime_name} {heartbeat.lifecycle_state.lower()} · phase {heartbeat.observed_phase}",
        )
        processed_control = self._process_control(backend)
        claim_started = time.monotonic()
        try:
            claimed = self.store.claim_simulation_run(
                worker_id=self.worker_id,
                adapter_id=self.profile.adapter_id,
                lease_ttl=self.lease_ttl,
                occurred_at=_utc_now(),
            )
        except WorkflowAuthoringUnavailable as exc:
            self._heartbeat(ready=False, detail=f"Simulation claim blocked: {exc}")
            return processed_control
        if claimed is None:
            return processed_control
        if not claimed["reconciliation"]:
            try:
                backend.assert_ready()
            except Exception as exc:
                self.store.finish_simulation_run(
                    claimed["run_id"],
                    worker_id=self.worker_id,
                    state="FAILED",
                    detail=f"{self.runtime_name} became unavailable before dispatch: {exc}",
                    progress=claimed["progress"],
                    result=None,
                    occurred_at=_utc_now(),
                )
                self._heartbeat(
                    ready=False,
                    detail=f"{self.runtime_name} unavailable before dispatch: {exc}",
                )
                return True
        self._heartbeat(ready=False, detail=f"Running {claimed['run_id']}")
        workflow = WorkflowDefinitionV2.model_validate(claimed["workflow"])
        issues = self.store.compatibility_issues(workflow, self.profile)
        if issues:
            self.store.finish_simulation_run(
                claimed["run_id"],
                worker_id=self.worker_id,
                state="FAILED",
                detail="; ".join(issues),
                progress=claimed["progress"],
                result=None,
                occurred_at=_utc_now(),
            )
            return True

        active_roles = self.config.roles
        if isinstance(self.profile, UnityAdapterProfileV2):
            routes, dispatches = resolve_unity_workflow(workflow, self.profile.dynamic_binding)
            backend.dispatches = dispatches
            backend.allowed_steps = {key: value.action.capability for key, value in dispatches.items()}
            scene_assets = {item.unity_binding_id: item for item in self.profile.dynamic_binding.scene.bindings}
            active_roles = {
                role: UnityRoleBindingV1(
                    asset_id=scene_assets[route.unity_binding_id].identity.asset_id,
                    agent_id=route.agent_id,
                ) for role, route in routes.items()
            }

        runtime_dir = self.runtime_root / claimed["run_id"]
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stop = Event()
        confirmed_lease_deadline = claim_started + self.lease_ttl.total_seconds()

        @contextmanager
        def command_fence():
            try:
                with self.store.simulation_dispatch_claim(claimed["run_id"], worker_id=self.worker_id):
                    if stop.is_set() or aborted.is_set() or time.monotonic() >= confirmed_lease_deadline:
                        raise WorkflowAuthoringConflict("Last confirmed Worker lease expired")
                    if isinstance(backend, IsaacSimulationFileBackend):
                        backend.refresh_permit(
                            worker_id=self.worker_id,
                            run_id=claimed["run_id"],
                            lease_deadline=confirmed_lease_deadline,
                        )
                    yield
            except WorkflowAuthoringConflict as exc:
                aborted.set()
                raise UnityRunAborted(str(exc)) from exc

        backend.command_fence = command_fence

        def keep_permit_alive() -> None:
            # Control acknowledgements and DB renewals can block; permission has
            # its own short loop, but every refresh still proves the DB claim.
            try:
                while not stop.is_set() and not aborted.is_set():
                    with command_fence():
                        pass
                    if stop.wait(PERMIT_REFRESH_SECONDS):
                        break
            except Exception as exc:
                aborted.set()
                print(f"Isaac Worker permit ended for {claimed['run_id']}: {exc}", flush=True)
            finally:
                backend.revoke_permit()

        def renew() -> None:
            nonlocal confirmed_lease_deadline
            interval = max(0.5, min(5.0, self.lease_ttl.total_seconds() / 3.0))
            next_renewal = time.monotonic()
            next_control_poll = time.monotonic()
            control_poll_interval = max(
                0.1, min(0.5, self.profile.poll_interval_seconds)
            )
            progress = claimed["progress"]
            last_renewal_error: str | None = None
            while not stop.wait(0.1):
                current = time.monotonic()
                if current >= confirmed_lease_deadline:
                    aborted.set()
                    return
                if current >= next_renewal:
                    try:
                        heartbeat = backend.read_heartbeat()
                        progress = self._progress_with_heartbeat(
                            runtime_dir, progress, heartbeat
                        )
                        self.store.update_simulation_progress(
                            claimed["run_id"],
                            worker_id=self.worker_id,
                            progress=progress,
                            detail=f"{self.runtime_name} simulation is {heartbeat.lifecycle_state.lower()}",
                            lease_ttl=self.lease_ttl,
                            occurred_at=_utc_now(),
                        )
                        confirmed_lease_deadline = current + self.lease_ttl.total_seconds()
                        self._heartbeat(
                            ready=False,
                            detail=(
                                f"{heartbeat.lifecycle_state.title()} {claimed['run_id']}"
                            ),
                        )
                        next_renewal = time.monotonic() + interval
                        last_renewal_error = None
                    except WorkflowAuthoringConflict as exc:
                        aborted.set()
                        print(
                            f"Simulation lease ownership ended for {claimed['run_id']}: {exc}",
                            flush=True,
                        )
                        return
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {exc}"
                        if message != last_renewal_error:
                            print(
                                f"Simulation lease renewal will retry for "
                                f"{claimed['run_id']}: {message}",
                                flush=True,
                            )
                            last_renewal_error = message

                if current < next_control_poll:
                    continue
                try:
                    self._process_control(
                        backend,
                        run_id=claimed["run_id"],
                        aborted=aborted,
                    )
                except Exception as exc:
                    print(
                        f"Simulation control polling will retry for "
                        f"{claimed['run_id']}: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                finally:
                    next_control_poll = time.monotonic() + control_poll_interval

        pulse = Thread(
            target=renew,
            name=f"{self.runtime_name.lower()}-lease-{claimed['run_id']}",
            daemon=True,
        )
        pulse.start()
        permit_pulse = None
        if isinstance(backend, IsaacSimulationFileBackend):
            permit_pulse = Thread(
                target=keep_permit_alive, name=f"isaac-permit-{claimed['run_id']}", daemon=True,
            )
            permit_pulse.start()
        try:
            active_config = self.config.model_copy(
                update={"bridge_root": str(backend.bridge_root), "roles": active_roles}
            )
            result = execute_scenario(
                config=active_config,
                workflow=workflow,
                backend=backend,
                runtime_dir=runtime_dir,
                run_id=claimed["run_id"],
                now=parse_datetime(claimed["started_at"]),
            )
            if aborted.is_set():
                return True
            heartbeat = backend.read_heartbeat()
            progress = self._progress_with_heartbeat(
                runtime_dir, claimed["progress"], heartbeat
            )
            orchestrator = result.get("run", {})
            orchestrator_state = orchestrator.get("state")
            completed = orchestrator_state == "COMPLETE"
            reason = orchestrator.get("reason") or orchestrator.get("terminal_reason")
            self.store.finish_simulation_run(
                claimed["run_id"],
                worker_id=self.worker_id,
                state="COMPLETE" if completed else "FAILED",
                detail=(
                    f"{self.runtime_name} simulation completed"
                    if completed
                    else f"{self.runtime_name} simulation ended in {orchestrator_state}: {reason or 'no reason'}"
                ),
                progress=progress,
                result={
                    "physical_output_connected": result["physical_output_connected"],
                    "completed_at": result["completed_at"],
                    "orchestrator_state": orchestrator_state,
                    "orchestrator_reason": reason,
                    **({
                        "execution_runtime": "isaac",
                        "runtime_kind": "isaac-sim-executor",
                        "profile_id": self.profile.profile_id,
                        "scene_sha256": self.profile.scene_sha256,
                        "session_id": backend.session_id,
                    } if isinstance(self.profile, IsaacAdapterProfileV3) else {}),
                    **({
                        "scene_manifest_sha256": self.profile.dynamic_binding.scene_sha256,
                        "asset_actions": {
                            step: {"asset_id": selected.binding.identity.asset_id,
                                   "unity_binding_id": selected.binding.unity_binding_id,
                                   "action_ref": selected.action.action_ref,
                                   "source_sha256": selected.action.script.sha256}
                            for step, selected in backend.dispatches.items()
                        },
                    } if isinstance(self.profile, UnityAdapterProfileV2) else {}),
                },
                occurred_at=_utc_now(),
            )
        except Exception as exc:
            if aborted.is_set():
                return True
            progress = _read_progress(runtime_dir, claimed["progress"])
            try:
                self.store.finish_simulation_run(
                    claimed["run_id"],
                    worker_id=self.worker_id,
                    state="FAILED",
                    detail=f"{self.runtime_name} Simulation failed: {exc}",
                    progress=progress,
                    result=None,
                    occurred_at=_utc_now(),
                )
            except WorkflowAuthoringConflict:
                self._heartbeat(
                    ready=False,
                    detail=f"Run ownership expired for {claimed['run_id']}",
                )
        finally:
            stop.set()
            pulse.join(timeout=2.0)
            if permit_pulse is not None:
                permit_pulse.join(timeout=2.0)
                backend.revoke_permit()
        return True


# Keep imports used by existing Unity deployments and tests source-compatible.
UnityWorkflowWorker = WorkflowSimulationWorker


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the output-disconnected Workflow queue Worker (Unity or Isaac profile)"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--adapter-profile", type=Path, required=True)
    parser.add_argument(
        "--skill-catalog",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument("--scenario-config", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--marker-path",
        type=Path,
        default=Path("/tmp/sang_unity_orchestrator_enabled"),
    )
    parser.add_argument("--worker-id", default="unity-scenario1-worker")
    parser.add_argument("--poll-interval-s", type=float, default=0.5)
    parser.add_argument("--lease-ttl-s", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args(args)
    if options.poll_interval_s <= 0.0:
        parser.error("poll-interval-s must be positive")
    if options.lease_ttl_s < 3.0:
        parser.error("lease-ttl-s must be at least 3 seconds")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    store = WorkflowAuthoringStore(
        options.db_path,
        adapter_profile_paths=[options.adapter_profile],
        skill_catalog_paths=options.skill_catalog,
    )
    worker = WorkflowSimulationWorker(
        store=store,
        worker_id=options.worker_id,
        adapter_profile_path=options.adapter_profile,
        scenario_config_path=options.scenario_config,
        runtime_root=options.runtime_root,
        marker_path=options.marker_path,
        lease_ttl=timedelta(seconds=options.lease_ttl_s),
    )
    if options.once:
        worker.run_once()
        return
    while True:
        worker.run_once()
        time.sleep(options.poll_interval_s)


if __name__ == "__main__":
    main()
