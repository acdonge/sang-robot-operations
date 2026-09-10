from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.store import FleetStore
from fleet_control_plane.task_bundle import load_task_bundle, register_task_bundle


ROOT = Path(__file__).parents[3]
BUNDLE_PATH = ROOT / "contracts" / "tasks" / "iitp-scenario1-shadow.v1.json"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class _ClientAdapter:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def get_configuration(self):
        response = self.client.get("/api/v1/configuration")
        assert response.status_code == 200, response.text
        return response.json()

    def post_json(self, path, payload):
        response = self.client.post(path, json=payload)
        assert response.status_code == 201, response.text
        return response.json()


def test_iitp_bundle_is_coherent_and_preserves_the_supplied_workflow():
    bundle = load_task_bundle(BUNDLE_PATH)

    assert bundle.unity_editor_version == "6000.5.6f1"
    assert [step.step_id for step in bundle.workflow] == [
        "mobile_delivery",
        "omy_pre_pick_place",
        "rb5_waam",
        "omy_post_pick_place",
        "mobile_return",
    ]
    assert bundle.task_run.state == "READY"
    access_by_role = {
        assignment.role: assignment.access_mode
        for assignment in bundle.task_run.assignments
    }
    assert access_by_role == {
        "mobile_carrier": "exclusive",
        "omy_material_handling": "exclusive",
        "rb5_waam_motion": "exclusive",
        "human_tracking": "shared_read",
        "workspace_vision": "shared_read",
        "close_range_vision": "shared_read",
        "welding_vision": "shared_read",
    }


def test_bundle_registration_is_idempotent(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    app = create_app(store, auth_enabled=False, now=lambda: NOW)
    client = TestClient(app)
    heartbeat = {
        "schema_version": 1,
        "agent_id": "processing-lab1",
        "agent_name": "Lab1 Processing PC",
        "sw_version": "0.1.0-test",
        "sent_at": NOW.isoformat(),
        "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 5},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "sync": {"supported": False, "state": "unsupported", "pending": None},
        "robots": [],
    }
    response = client.post("/api/v1/agents/heartbeat", json=heartbeat)
    assert response.status_code == 202, response.text
    response = client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "processing-lab1",
            "display_name": "Lab1 Processing PC",
            "agent_ids": ["processing-lab1"],
        },
    )
    assert response.status_code == 201, response.text

    bundle = load_task_bundle(BUNDLE_PATH)
    adapter = _ClientAdapter(client)
    first = register_task_bundle(bundle, adapter)
    second = register_task_bundle(bundle, adapter)

    assert len(first["created"]) == 10
    assert first["reused"] == []
    assert second["created"] == []
    assert len(second["reused"]) == 10
    configuration = client.get("/api/v1/configuration").json()
    run = next(
        item
        for item in configuration["task_runs"]
        if item["run_id"] == bundle.task_run.run_id
    )
    assert run["state"] == "READY"
    assert len(run["assignments"]) == 7
