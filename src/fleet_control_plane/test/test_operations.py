from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def observed_heartbeat():
    topic = {
        "age_ms": 20.0,
        "rate_hz": 20.0,
        "minimum_rate_hz": 10.0,
        "rate_ok": True,
        "seq": 10,
        "fresh": True,
    }
    return {
        "schema_version": 1,
        "agent_id": "agent-a",
        "agent_name": "Processing A",
        "sw_version": "0.1.0",
        "sent_at": NOW.isoformat(),
        "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 10},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "sync": {"supported": False, "state": "unsupported", "pending": None},
        "robots": [
            {
                "robot_id": "robot-a",
                "model": "dual_arm",
                "fsm_state": "READY",
                "phase": "monitoring",
                "sources_fresh": True,
                "rates_ok": True,
                "sources": [
                    {
                        "key": "observation.state",
                        "kind": "state",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "topics": [{**topic, "topic": "/robot/joint_states"}],
                    },
                    {
                        "key": "cam_a",
                        "kind": "camera",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "preview_url": "http://127.0.0.1:8101/cam_a.mjpg",
                        "topics": [{**topic, "topic": "/camera/a/compressed"}],
                    },
                    {
                        "key": "cam_b",
                        "kind": "camera",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "preview_url": "http://127.0.0.1:8101/cam_b.mjpg",
                        "topics": [{**topic, "topic": "/camera/b/compressed"}],
                    },
                ],
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


def runtime_heartbeat(runtime_kind, sequence):
    payload = observed_heartbeat()
    payload.update(
        {
            "schema_version": 3,
            "boot_id": "019fce1c-a4b5-7ba2-b0d5-01ea4940926d",
            "sequence": sequence,
        }
    )
    payload["robots"][0]["runtime_kind"] = runtime_kind
    return AgentHeartbeatV1.model_validate(payload)


def make_client(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    app = create_app(store, now=lambda: NOW, websocket_interval_s=0.01)
    client = TestClient(app)
    assert client.post(
        "/api/v1/agents/heartbeat", json=observed_heartbeat()
    ).status_code == 202
    return client, store


def create_node(client):
    response = client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "node-a",
            "display_name": "Processing Node A",
            "agent_ids": ["agent-a"],
        },
    )
    assert response.status_code == 201, response.text


def create_bound_asset(client, asset_id, asset_type, source_key=None):
    response = client.post(
        "/api/v1/config/assets",
        json={
            "asset_id": asset_id,
            "asset_type": asset_type,
            "display_name": asset_id,
            "node_id": "node-a",
            "binding": {
                "agent_id": "agent-a",
                "robot_id": "robot-a",
                "source_key": source_key,
            },
            "capabilities": ["telemetry"],
        },
    )
    assert response.status_code == 201, response.text


def test_discovery_registration_and_configuration_survive_reopen(tmp_path):
    client, store = make_client(tmp_path)
    discovery = client.get("/api/v1/config/discovery").json()
    assert [item["robot_id"] for item in discovery["robots"]] == ["robot-a"]
    assert {item["source_key"] for item in discovery["cameras"]} == {
        "cam_a",
        "cam_b",
    }

    create_node(client)
    create_bound_asset(client, "arm-a", "robot")
    create_bound_asset(client, "camera-a", "camera", "cam_a")
    response = client.post(
        "/api/v1/config/assets",
        json={
            "asset_id": "welder-a",
            "asset_type": "welder",
            "display_name": "Welder A",
            "node_id": "node-a",
            "capabilities": ["tig"],
        },
    )
    assert response.status_code == 201

    discovery = client.get("/api/v1/config/discovery").json()
    assert discovery["robots"] == []
    assert [item["source_key"] for item in discovery["cameras"]] == ["cam_b"]

    response = client.post(
        "/api/v1/config/workcells",
        json={
            "workcell_id": "weld-cell",
            "display_name": "Weld Cell",
            "description": "Cooperative welding cell",
            "asset_ids": ["arm-a", "camera-a", "welder-a"],
        },
    )
    assert response.status_code == 201
    response = client.post(
        "/api/v1/config/task-definitions",
        json={
            "task_definition_id": "plate-weld",
            "version": 1,
            "display_name": "Plate Weld",
            "roles": [
                {"role": "handler", "asset_type": "robot", "access_mode": "exclusive"},
                {"role": "vision", "asset_type": "camera", "access_mode": "shared_read"},
                {"role": "process", "asset_type": "welder", "access_mode": "exclusive"},
            ],
        },
    )
    assert response.status_code == 201
    response = client.post(
        "/api/v1/config/task-runs",
        json={
            "run_id": "plate-weld-001",
            "task_definition_id": "plate-weld",
            "task_version": 1,
            "workcell_id": "weld-cell",
            "display_name": "Plate Weld 001",
            "state": "READY",
            "assignments": [
                {"role": "handler", "asset_id": "arm-a", "access_mode": "exclusive"},
                {"role": "vision", "asset_id": "camera-a", "access_mode": "shared_read"},
                {"role": "process", "asset_id": "welder-a", "access_mode": "exclusive"},
            ],
        },
    )
    assert response.status_code == 201, response.text

    configuration = FleetStore(store.path).get_configuration()
    assert configuration["processing_nodes"][0]["agent_ids"] == ["agent-a"]
    assert len(configuration["assets"]) == 3
    assert configuration["workcells"][0]["asset_ids"] == [
        "arm-a",
        "camera-a",
        "welder-a",
    ]
    assert configuration["task_runs"][0]["state"] == "READY"
    assert all(
        assignment["reserved"]
        for assignment in configuration["task_runs"][0]["assignments"]
    )
    assert len(configuration["audit"]) == 7


def test_task_versions_are_immutable_and_contiguous(tmp_path):
    client, _ = make_client(tmp_path)
    body = {
        "task_definition_id": "inspect",
        "version": 1,
        "display_name": "Inspect",
        "roles": [
            {"role": "camera", "asset_type": "camera", "access_mode": "shared_read"}
        ],
    }
    assert client.post("/api/v1/config/task-definitions", json=body).status_code == 201
    assert client.post("/api/v1/config/task-definitions", json=body).status_code == 409
    body["version"] = 3
    assert client.post("/api/v1/config/task-definitions", json=body).status_code == 409
    body["version"] = 2
    assert client.post("/api/v1/config/task-definitions", json=body).status_code == 201


def test_ready_validation_and_database_reservation_guard(tmp_path):
    client, _ = make_client(tmp_path)
    create_node(client)
    create_bound_asset(client, "camera-a", "camera", "cam_a")
    create_bound_asset(client, "arm-a", "robot")
    assert client.post(
        "/api/v1/config/workcells",
        json={
            "workcell_id": "cell-a",
            "display_name": "Cell A",
            "asset_ids": ["camera-a"],
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/task-definitions",
        json={
            "task_definition_id": "inspect",
            "version": 1,
            "display_name": "Inspect",
            "roles": [
                {"role": "vision", "asset_type": "camera", "access_mode": "shared_read"}
            ],
        },
    ).status_code == 201

    base = {
        "task_definition_id": "inspect",
        "task_version": 1,
        "workcell_id": "cell-a",
        "display_name": "Inspection",
        "state": "READY",
    }
    missing = client.post(
        "/api/v1/config/task-runs",
        json={**base, "run_id": "missing", "assignments": []},
    )
    assert missing.status_code == 422
    wrong_type = client.post(
        "/api/v1/config/task-runs",
        json={
            **base,
            "run_id": "wrong-type",
            "assignments": [
                {"role": "vision", "asset_id": "arm-a", "access_mode": "shared_read"}
            ],
        },
    )
    assert wrong_type.status_code == 422

    assignment = [
        {"role": "vision", "asset_id": "camera-a", "access_mode": "shared_read"}
    ]
    first = client.post(
        "/api/v1/config/task-runs",
        json={**base, "run_id": "ready-1", "assignments": assignment},
    )
    assert first.status_code == 201
    second = client.post(
        "/api/v1/config/task-runs",
        json={**base, "run_id": "ready-2", "assignments": assignment},
    )
    assert second.status_code == 409
    assert "already reserved" in second.json()["error"]


def test_run_scoped_fleet_returns_only_assigned_camera_source(tmp_path):
    client, _ = make_client(tmp_path)
    create_node(client)
    create_bound_asset(client, "camera-a", "camera", "cam_a")
    assert client.post(
        "/api/v1/config/task-definitions",
        json={
            "task_definition_id": "camera-check",
            "version": 1,
            "display_name": "Camera Check",
            "roles": [
                {"role": "vision", "asset_type": "camera", "access_mode": "shared_read"}
            ],
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/task-runs",
        json={
            "run_id": "camera-check-001",
            "task_definition_id": "camera-check",
            "task_version": 1,
            "display_name": "Camera Check 001",
            "state": "DRAFT",
            "assignments": [
                {"role": "vision", "asset_id": "camera-a", "access_mode": "shared_read"}
            ],
        },
    ).status_code == 201

    degraded = observed_heartbeat()
    degraded["robots"][0]["sources_fresh"] = False
    cam_b = degraded["robots"][0]["sources"][2]
    cam_b["fresh"] = False
    cam_b["topics"][0]["fresh"] = False
    cam_b["topics"][0]["age_ms"] = 1500.0
    assert client.post(
        "/api/v1/agents/heartbeat", json=degraded
    ).status_code == 202

    payload = client.get("/api/v1/task-runs/camera-check-001/fleet").json()
    assert payload["type"] == "task_run_fleet"
    assert [agent["agent_id"] for agent in payload["agents"]] == ["agent-a"]
    sources = payload["agents"][0]["robots"][0]["sources"]
    assert [source["key"] for source in sources] == ["cam_a"]
    assert sources[0]["preview_url"].endswith("cam_a.mjpg")
    assert payload["assets"][0]["state"] == "HEALTHY"
    assert all(event["source_key"] != "cam_b" for event in payload["events"])
    assert payload["agents"][0]["event_counts"] == {"agent_online": 1}
    assert "recordings" not in payload["agents"][0]
    assert "recordings" not in payload["summary"]

    with client.websocket_connect(
        "/api/v1/task-runs/camera-check-001/stream"
    ) as websocket:
        streamed = websocket.receive_json()
        assert streamed["task_run"]["run_id"] == "camera-check-001"
        assert [source["key"] for source in streamed["agents"][0]["robots"][0]["sources"]] == [
            "cam_a"
        ]


def test_observed_binding_requires_registered_node_membership(tmp_path):
    client, _ = make_client(tmp_path)
    without_node = client.post(
        "/api/v1/config/assets",
        json={
            "asset_id": "camera-a",
            "asset_type": "camera",
            "display_name": "Camera A",
            "binding": {
                "agent_id": "agent-a",
                "robot_id": "robot-a",
                "source_key": "cam_a",
            },
        },
    )
    assert without_node.status_code == 422

    unknown_agent = client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "node-missing",
            "display_name": "Missing",
            "agent_ids": ["agent-missing"],
        },
    )
    assert unknown_agent.status_code == 404


def test_asset_runtime_kind_rejects_mismatch_and_detects_later_drift(tmp_path):
    client, store = make_client(tmp_path)
    store.upsert(runtime_heartbeat("physical", 1), received_at=NOW)
    create_node(client)
    body = {
        "asset_id": "arm-a",
        "asset_type": "robot",
        "display_name": "Arm A",
        "runtime_kind": "simulation",
        "node_id": "node-a",
        "binding": {
            "agent_id": "agent-a",
            "robot_id": "robot-a",
        },
        "capabilities": ["telemetry"],
    }

    mismatch = client.post("/api/v1/config/assets", json=body)
    assert mismatch.status_code == 422
    assert "differs from observed" in mismatch.json()["error"]

    body["runtime_kind"] = "physical"
    created = client.post("/api/v1/config/assets", json=body)
    assert created.status_code == 201
    assert created.json()["runtime_kind"] == "physical"

    correction_mismatch = client.patch(
        "/api/v1/config/assets/arm-a/runtime-kind",
        json={"runtime_kind": "simulation"},
    )
    assert correction_mismatch.status_code == 422
    cleared = client.patch(
        "/api/v1/config/assets/arm-a/runtime-kind",
        json={"runtime_kind": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["runtime_kind"] is None
    restored = client.patch(
        "/api/v1/config/assets/arm-a/runtime-kind",
        json={"runtime_kind": "physical"},
    )
    assert restored.status_code == 200

    store.upsert(
        runtime_heartbeat("simulation", 2),
        received_at=NOW + timedelta(seconds=1),
    )
    drift = next(
        event
        for event in store.list_events()
        if event["event_type"] == "runtime_kind_drift"
    )
    assert drift["severity"] == "critical"
    assert drift["details"] == {
        "asset_id": "arm-a",
        "declared_runtime_kind": "physical",
        "observed_runtime_kind": "simulation",
    }

    store.upsert(
        runtime_heartbeat("physical", 3),
        received_at=NOW + timedelta(seconds=2),
    )
    assert store.list_events()[0]["event_type"] == "runtime_kind_recovered"
