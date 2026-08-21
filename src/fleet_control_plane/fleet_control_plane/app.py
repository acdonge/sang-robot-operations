"""FastAPI application for the read-only Central Control Plane MVP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    AgentRegistrationUpdate,
    AssetCreate,
    AssetRuntimeKindUpdate,
    ConnectionProfileCreate,
    ConnectionProfileReplace,
    CredentialRevoke,
    ProcessingNodeCreate,
    TaskDefinitionCreate,
    TaskRunCreate,
    WorkcellCreate,
)
from fleet_control_plane.status import (
    FleetTTLs,
    isoformat_z,
    present_agent,
    summarize_agents,
    utc_now,
)
from fleet_control_plane.store import (
    ConfigurationConflict,
    ConfigurationInvalid,
    ConfigurationNotFound,
    FleetStore,
)


HEARTBEAT_SCHEMA_VERSION = 1
MAX_HEARTBEAT_BYTES = 256 * 1024
ALLOWED_ADMIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})


def _source_rate_healthy(source: dict[str, Any]) -> bool:
    has_rate_contract = any(
        topic.get("minimum_rate_hz") is not None
        for topic in source.get("topics", [])
    )
    return not has_rate_contract or source.get("rate_ok") is True


class FleetAPIError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


def create_app(
    store: FleetStore,
    *,
    ui_dir: str | Path | None = None,
    operator_ui_dir: str | Path | None = None,
    ttls: FleetTTLs = FleetTTLs(),
    now: Callable[[], Any] = utc_now,
    websocket_interval_s: float = 1.0,
) -> FastAPI:
    if websocket_interval_s <= 0.0:
        raise ValueError("websocket_interval_s must be positive")

    app = FastAPI(title="SANG Fleet Control Plane", version="0.1.0")
    app.state.fleet_revision = 0

    @app.middleware("http")
    async def reject_oversized_heartbeat(request: Request, call_next):
        if request.url.hostname not in ALLOWED_ADMIN_HOSTS:
            return JSONResponse(
                status_code=421,
                content={"error": "Admin API requires a loopback Host header"},
            )
        if request.method == "POST" and request.url.path == "/api/v1/agents/heartbeat":
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is not None and content_length > MAX_HEARTBEAT_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "heartbeat body exceeds 256 KiB"},
                )
        return await call_next(request)

    @app.exception_handler(FleetAPIError)
    async def fleet_error_handler(request: Request, exc: FleetAPIError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    @app.exception_handler(ConfigurationNotFound)
    async def configuration_not_found_handler(
        request: Request,
        exc: ConfigurationNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(ConfigurationConflict)
    async def configuration_conflict_handler(
        request: Request,
        exc: ConfigurationConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(ConfigurationInvalid)
    async def configuration_invalid_handler(
        request: Request,
        exc: ConfigurationInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    def agents_at(current) -> list[dict[str, Any]]:
        agents = [
            present_agent(snapshot, now=current, ttls=ttls)
            for snapshot in store.list_snapshots()
        ]
        transitions = store.reconcile_agent_statuses(
            agents,
            occurred_at=current,
        )
        if transitions:
            app.state.fleet_revision += transitions
        event_counts = store.event_counts_by_agent()
        for agent in agents:
            agent["event_counts"] = event_counts.get(agent["agent_id"], {})
        return agents

    def agents_payload() -> list[dict[str, Any]]:
        return agents_at(now())

    def shadow_agents_payload() -> list[dict[str, Any]]:
        current = now()
        return [
            present_agent(snapshot, now=current, ttls=ttls)
            for snapshot in store.list_snapshots()
        ]

    def fleet_payload() -> dict[str, Any]:
        current = now()
        agents = agents_at(current)
        return {
            "type": "fleet",
            "agents": agents,
            "summary": summarize_agents(agents, now=current),
            "events": store.list_events(limit=100),
        }

    def configuration_discovery() -> dict[str, Any]:
        assets = store.list_assets()
        claimed = {
            (
                asset["binding"]["agent_id"],
                asset["binding"]["robot_id"],
                asset["binding"]["source_key"],
            )
            for asset in assets
            if asset["binding"] is not None
        }
        node_by_agent = {
            agent_id: node["node_id"]
            for node in store.list_processing_nodes()
            for agent_id in node["agent_ids"]
        }
        robots = []
        cameras = []
        for agent in agents_payload():
            for robot in agent["robots"]:
                robot_binding = (agent["agent_id"], robot["robot_id"], None)
                if robot_binding not in claimed:
                    robots.append(
                        {
                            "agent_id": agent["agent_id"],
                            "agent_name": agent["agent_name"],
                            "node_id": node_by_agent.get(agent["agent_id"]),
                            "robot_id": robot["robot_id"],
                            "model": robot["model"],
                            "runtime_kind": robot.get("runtime_kind"),
                            "status": agent["status"],
                        }
                    )
                for source in robot["sources"]:
                    if source["kind"] != "camera":
                        continue
                    camera_binding = (
                        agent["agent_id"],
                        robot["robot_id"],
                        source["key"],
                    )
                    if camera_binding in claimed:
                        continue
                    cameras.append(
                        {
                            "agent_id": agent["agent_id"],
                            "agent_name": agent["agent_name"],
                            "node_id": node_by_agent.get(agent["agent_id"]),
                            "robot_id": robot["robot_id"],
                            "runtime_kind": robot.get("runtime_kind"),
                            "source_key": source["key"],
                            "preview_url": source.get("preview_url"),
                            "fresh": source["fresh"],
                            "rate_ok": source.get("rate_ok"),
                            "status": agent["status"],
                        }
                    )
        return {"robots": robots, "cameras": cameras}

    def task_run_or_404(run_id: str) -> dict[str, Any]:
        task_run = store.get_task_run(run_id)
        if task_run is None:
            raise ConfigurationNotFound(f"unknown task run: {run_id}")
        return task_run

    def filter_agent_for_run(
        agent: dict[str, Any],
        selected: dict[str, set[str] | bool],
    ) -> dict[str, Any] | None:
        robots = []
        for robot in agent["robots"]:
            robot_selection = selected.get(robot["robot_id"])
            if robot_selection is None:
                continue
            robot_view = {**robot}
            if robot_selection is not True:
                robot_view["sources"] = [
                    source
                    for source in robot["sources"]
                    if source["key"] in robot_selection
                ]
                robot_view["sources_fresh"] = all(
                    source["fresh"]
                    for source in robot_view["sources"]
                    if source["required"]
                )
                robot_view["rates_ok"] = all(
                    _source_rate_healthy(source)
                    for source in robot_view["sources"]
                    if source["required"]
                )
            robots.append(robot_view)
        if not robots:
            return None
        run_view = {**agent, "robots": robots, "event_counts": {}}
        run_view.pop("recordings", None)
        return run_view

    def run_asset_observations(
        task_run: dict[str, Any],
        agents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        agents_by_id = {agent["agent_id"]: agent for agent in agents}
        observations = []
        for assignment in task_run["assignments"]:
            asset = assignment["asset"]
            binding = asset["binding"]
            state = "UNINSTRUMENTED"
            detail = "No live Agent binding"
            if binding is not None:
                agent = agents_by_id.get(binding["agent_id"])
                if agent is None or agent["status"] == "offline":
                    state = "OFFLINE"
                    detail = "Agent heartbeat is offline"
                else:
                    robot = next(
                        (
                            item
                            for item in agent["robots"]
                            if item["robot_id"] == binding["robot_id"]
                        ),
                        None,
                    )
                    if robot is None:
                        state = "UNAVAILABLE"
                        detail = "Robot is absent from the latest heartbeat"
                    elif (
                        asset.get("runtime_kind") is not None
                        and robot.get("runtime_kind") is not None
                        and asset["runtime_kind"] != robot["runtime_kind"]
                    ):
                        state = "IDENTITY_MISMATCH"
                        detail = (
                            f"Asset is {asset['runtime_kind']}; observed runtime is "
                            f"{robot['runtime_kind']}"
                        )
                    elif binding["source_key"] is None:
                        state = (
                            "HEALTHY"
                            if agent["status"] == "online"
                            and robot["sources_fresh"]
                            and robot["rates_ok"]
                            and not robot["fault"]
                            else "DEGRADED"
                        )
                        detail = robot["fault"] or robot["phase"]
                    else:
                        source = next(
                            (
                                item
                                for item in robot["sources"]
                                if item["key"] == binding["source_key"]
                            ),
                            None,
                        )
                        if source is None:
                            state = "UNAVAILABLE"
                            detail = "Camera source is absent from the latest heartbeat"
                        else:
                            state = (
                                "HEALTHY"
                                if agent["status"] == "online"
                                and source["fresh"]
                                and _source_rate_healthy(source)
                                else "DEGRADED"
                            )
                            detail = source["key"]
            observations.append(
                {
                    "role": assignment["role"],
                    "access_mode": assignment["access_mode"],
                    "reserved": assignment["reserved"],
                    "asset": asset,
                    "state": state,
                    "detail": detail,
                }
            )
        return observations

    def task_run_fleet_payload(run_id: str) -> dict[str, Any]:
        task_run = task_run_or_404(run_id)
        selection: dict[str, dict[str, set[str] | bool]] = {}
        assigned_agent_ids = set()
        for assignment in task_run["assignments"]:
            binding = assignment["asset"]["binding"]
            if binding is None:
                continue
            agent_id = binding["agent_id"]
            robot_id = binding["robot_id"]
            assigned_agent_ids.add(agent_id)
            robot_selection = selection.setdefault(agent_id, {})
            if binding["source_key"] is None:
                robot_selection[robot_id] = True
            elif robot_selection.get(robot_id) is not True:
                sources = robot_selection.setdefault(robot_id, set())
                assert isinstance(sources, set)
                sources.add(binding["source_key"])

        current = now()
        all_agents = agents_at(current)
        filtered_agents = []
        for agent in all_agents:
            selected = selection.get(agent["agent_id"])
            if selected is None:
                continue
            filtered = filter_agent_for_run(agent, selected)
            if filtered is not None:
                filtered_agents.append(filtered)
        def event_is_in_scope(event: dict[str, Any]) -> bool:
            selected = selection.get(event["agent_id"])
            if selected is None:
                return False
            if event["robot_id"] is None:
                return True
            robot_selection = selected.get(event["robot_id"])
            if robot_selection is True:
                return True
            return (
                isinstance(robot_selection, set)
                and event["source_key"] in robot_selection
            )

        scoped_events = [
            event
            for event in store.list_events(limit=500)
            if event_is_in_scope(event)
        ][:100]
        scoped_event_counts: dict[str, dict[str, int]] = {}
        for event in scoped_events:
            counts = scoped_event_counts.setdefault(event["agent_id"], {})
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
        for agent in filtered_agents:
            agent["event_counts"] = scoped_event_counts.get(agent["agent_id"], {})
        summary = summarize_agents(filtered_agents, now=current)
        summary.pop("recordings", None)
        return {
            "type": "task_run_fleet",
            "generated_at": isoformat_z(current),
            "task_run": task_run,
            "assets": run_asset_observations(task_run, all_agents),
            "agents": filtered_agents,
            "summary": summary,
            "events": scoped_events,
        }

    @app.get("/api/v1/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "scope": "loopback-configuration-mvp"}

    @app.post("/api/v1/agents/heartbeat", status_code=202)
    async def receive_heartbeat(body: AgentHeartbeatV1) -> dict[str, Any]:
        if body.schema_version != HEARTBEAT_SCHEMA_VERSION:
            raise FleetAPIError(
                400,
                f"unsupported schema_version: {body.schema_version}; "
                f"expected {HEARTBEAT_SCHEMA_VERSION}",
            )
        if store.agent_is_registered(body.agent_id):
            raise FleetAPIError(
                403,
                "registered remote Agents must use the dedicated mTLS ingress",
            )
        received_at = now()
        agents_at(received_at)
        transitions = store.upsert(body, received_at=received_at)
        app.state.fleet_revision += 1 + transitions
        return {
            "received_at": isoformat_z(received_at),
            "next_interval_s": 5,
        }

    @app.get("/api/v1/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return agents_payload()

    @app.get("/api/v1/shadow/agents")
    async def list_shadow_agents() -> list[dict[str, Any]]:
        return shadow_agents_payload()

    @app.get("/api/v1/summary")
    async def fleet_summary() -> dict[str, Any]:
        current = now()
        agents = agents_at(current)
        return summarize_agents(agents, now=current)

    @app.get("/api/v1/events")
    async def list_health_events(
        limit: int = Query(default=100, ge=1, le=500),
        agent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ) -> list[dict[str, Any]]:
        agents_at(now())
        return store.list_events(limit=limit, agent_id=agent_id)

    @app.get("/api/v1/configuration")
    async def get_configuration() -> dict[str, Any]:
        return store.get_configuration()

    @app.get("/api/v1/config/discovery")
    async def get_configuration_discovery() -> dict[str, Any]:
        return configuration_discovery()

    @app.get("/api/v1/config/security")
    async def get_security_configuration() -> dict[str, Any]:
        return {
            "schema_versions": store.schema_versions(),
            "agent_registrations": store.list_agent_registrations(),
            "agent_credentials": store.list_agent_credentials(),
            "connection_profiles": store.list_connection_profiles(),
            "connection_observations": store.list_connection_observations(),
        }

    @app.post("/api/v1/config/agent-registrations", status_code=201)
    async def create_agent_registration(
        body: AgentRegistrationCreate,
    ) -> dict[str, Any]:
        result = store.create_agent_registration(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/agent-registrations/{agent_id}")
    async def update_agent_registration(
        agent_id: str,
        body: AgentRegistrationUpdate,
    ) -> dict[str, Any]:
        result = store.update_agent_registration(agent_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/agent-credentials", status_code=201)
    async def create_agent_credential(
        body: AgentCredentialCreate,
    ) -> dict[str, Any]:
        result = store.create_agent_credential(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/agent-credentials/{credential_id}/revoke")
    async def revoke_agent_credential(
        credential_id: str,
        body: CredentialRevoke,
    ) -> dict[str, Any]:
        result = store.revoke_agent_credential(
            credential_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/connection-profiles", status_code=201)
    async def create_connection_profile(
        body: ConnectionProfileCreate,
    ) -> dict[str, Any]:
        result = store.create_connection_profile(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/connection-profiles/{profile_id}")
    async def replace_connection_profile(
        profile_id: str,
        body: ConnectionProfileReplace,
    ) -> dict[str, Any]:
        result = store.replace_connection_profile(profile_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/processing-nodes", status_code=201)
    async def create_processing_node(body: ProcessingNodeCreate) -> dict[str, Any]:
        result = store.create_processing_node(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/assets", status_code=201)
    async def create_asset(body: AssetCreate) -> dict[str, Any]:
        result = store.create_asset(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.patch("/api/v1/config/assets/{asset_id}/runtime-kind")
    async def set_asset_runtime_kind(
        asset_id: str,
        body: AssetRuntimeKindUpdate,
    ) -> dict[str, Any]:
        result = store.set_asset_runtime_kind(
            asset_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/workcells", status_code=201)
    async def create_workcell(body: WorkcellCreate) -> dict[str, Any]:
        result = store.create_workcell(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/task-definitions", status_code=201)
    async def create_task_definition(body: TaskDefinitionCreate) -> dict[str, Any]:
        result = store.create_task_definition(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/task-runs", status_code=201)
    async def create_task_run(body: TaskRunCreate) -> dict[str, Any]:
        result = store.create_task_run(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/task-runs/{run_id}")
    async def get_task_run(run_id: str) -> dict[str, Any]:
        return task_run_or_404(run_id)

    @app.get("/api/v1/task-runs/{run_id}/fleet")
    async def get_task_run_fleet(run_id: str) -> dict[str, Any]:
        return task_run_fleet_payload(run_id)

    @app.websocket("/api/v1/fleet")
    async def fleet_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        last_revision = -1
        next_periodic = 0.0
        try:
            while True:
                current_loop_time = time.monotonic()
                revision = app.state.fleet_revision
                if revision != last_revision or current_loop_time >= next_periodic:
                    payload = fleet_payload()
                    await websocket.send_json(payload)
                    last_revision = app.state.fleet_revision
                    next_periodic = current_loop_time + websocket_interval_s
                await asyncio.sleep(min(0.1, websocket_interval_s))
        except (RuntimeError, WebSocketDisconnect):
            pass

    @app.websocket("/api/v1/task-runs/{run_id}/stream")
    async def task_run_fleet_websocket(websocket: WebSocket, run_id: str) -> None:
        if store.get_task_run(run_id) is None:
            await websocket.close(code=4404, reason="unknown task run")
            return
        await websocket.accept()
        last_revision = -1
        next_periodic = 0.0
        try:
            while True:
                current_loop_time = time.monotonic()
                revision = app.state.fleet_revision
                if revision != last_revision or current_loop_time >= next_periodic:
                    await websocket.send_json(task_run_fleet_payload(run_id))
                    last_revision = app.state.fleet_revision
                    next_periodic = current_loop_time + websocket_interval_s
                await asyncio.sleep(min(0.1, websocket_interval_s))
        except (RuntimeError, WebSocketDisconnect):
            pass

    if operator_ui_dir is not None:
        operator_path = Path(operator_ui_dir)
        if not operator_path.is_dir():
            raise ValueError(f"Operator UI directory does not exist: {operator_path}")
        app.mount(
            "/operator",
            StaticFiles(directory=str(operator_path), html=True),
            name="operator-ui",
        )

    if ui_dir is not None:
        path = Path(ui_dir)
        if not path.is_dir():
            raise ValueError(f"Admin UI directory does not exist: {path}")
        app.mount("/", StaticFiles(directory=str(path), html=True), name="admin-ui")

    return app
