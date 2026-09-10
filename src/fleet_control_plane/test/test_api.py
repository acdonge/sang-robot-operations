from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.status import FleetTTLs
from fleet_control_plane.store import FleetStore


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += timedelta(seconds=seconds)


def heartbeat(
    agent_id="agent-1",
    *,
    state="READY",
    fresh=True,
    rates_ok=True,
    rate_hz=19.8,
    fault=None,
):
    return {
        "schema_version": 1,
        "agent_id": agent_id,
        "agent_name": f"Name {agent_id}",
        "sw_version": "0.1.0",
        "sent_at": "2026-07-21T12:00:00Z",
        "storage": {"disk_free_gb": 50, "ram_free_gb": 10, "cpu_pct": 20},
        "datasets": {"count": 2, "total_episodes": 8, "total_size_mb": 100},
        "recordings": {
            "enabled": True,
            "state": "ready",
            "error_code": None,
            "complete_count": 1,
            "failed_count": 0,
            "partial_count": 0,
            "invalid_count": 0,
            "scanned_count": 1,
            "total_size_bytes": 1024,
            "truncated": False,
            "recent": [],
        },
        "sync": {"supported": False, "state": "unsupported", "pending": None},
        "robots": [
            {
                "robot_id": f"robot-{agent_id}",
                "model": "test_robot",
                "fsm_state": state,
                "phase": state,
                "sources_fresh": fresh,
                "rates_ok": rates_ok,
                "sources": [
                    {
                        "key": "observation.state",
                        "kind": "state",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": fresh,
                        "rate_ok": rates_ok,
                        "topics": [
                            {
                                "topic": "/robot/joint_states",
                                "age_ms": 40.0 if fresh else 1400.0,
                                "rate_hz": rate_hz,
                                "minimum_rate_hz": 18.0,
                                "rate_ok": rates_ok,
                                "seq": 12,
                                "fresh": fresh,
                            }
                        ],
                    }
                ],
                "fault": fault,
                "recording": {
                    "active": state == "RECORDING",
                    "episode_index": 1,
                    "episodes_total": 5,
                    "frames": 120,
                    "elapsed_s": 4.0,
                    "current_instruction": "test",
                },
            }
        ],
    }


def make_client(tmp_path, clock=None):
    clock = clock or Clock()
    store = FleetStore(tmp_path / "fleet.db")
    app = create_app(
        store,
        now=clock,
        ttls=FleetTTLs(online_s=12, offline_s=30),
        websocket_interval_s=0.02,
        auth_enabled=False,
    )
    return TestClient(app), store, clock


def test_heartbeat_agents_and_summary(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.post("/api/v1/agents/heartbeat", json=heartbeat())
    assert response.status_code == 202
    assert response.json() == {
        "received_at": "2026-07-21T12:00:00.000Z",
        "next_interval_s": 5,
    }

    agents = client.get("/api/v1/agents").json()
    assert len(agents) == 1
    assert agents[0]["status"] == "online"
    assert agents[0]["sync"]["supported"] is False
    assert agents[0]["robots"][0]["sources"][0]["topics"][0]["seq"] == 12
    assert agents[0]["robots"][0]["sources"][0]["topics"][0]["rate_hz"] == 19.8
    assert agents[0]["event_counts"] == {"agent_online": 1}
    assert agents[0]["recordings"]["complete_count"] == 1

    summary = client.get("/api/v1/summary").json()
    assert summary["agents"] == {
        "online": 1,
        "stale": 0,
        "offline": 0,
        "needs_attention": 0,
        "total": 1,
    }
    assert summary["robots"]["total"] == 1
    assert summary["datasets"] == {"total_count": 2, "total_episodes": 8}
    assert summary["recordings"]["complete"] == 1


def test_legacy_heartbeat_rejects_non_loopback_clients(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    app = create_app(store, auth_enabled=False)
    client = TestClient(app, client=("198.51.100.20", 51000))

    response = client.post("/api/v1/agents/heartbeat", json=heartbeat())

    assert response.status_code == 403
    assert "mTLS ingress" in response.json()["error"]
    assert store.list_snapshots() == []


def test_shadow_agents_endpoint_does_not_reconcile_or_write_events(
    tmp_path,
    monkeypatch,
):
    client, store, _ = make_client(tmp_path)
    assert client.post("/api/v1/agents/heartbeat", json=heartbeat()).status_code == 202
    events_before = store.list_events()

    def fail_if_called(*args, **kwargs):
        del args, kwargs
        raise AssertionError("shadow endpoint must not reconcile Agent status")

    monkeypatch.setattr(store, "reconcile_agent_statuses", fail_if_called)
    response = client.get("/api/v1/shadow/agents")

    assert response.status_code == 200
    assert response.json()[0]["status"] == "online"
    assert "event_counts" not in response.json()[0]
    assert store.list_events() == events_before


def test_unregistered_camera_preview_upstream_is_not_exposed(tmp_path):
    client, _, _ = make_client(tmp_path)
    payload = heartbeat()
    source = payload["robots"][0]["sources"][0]
    source["key"] = "top"
    source["kind"] = "camera"
    source["preview_url"] = (
        "http://127.0.0.1:8101/robots/robot-agent-1/cameras/top.mjpg"
    )

    assert client.post("/api/v1/agents/heartbeat", json=payload).status_code == 202
    stored_source = client.get("/api/v1/agents").json()[0]["robots"][0]["sources"][0]
    assert stored_source["preview_url"] is None
    assert stored_source["upstream_preview_available"] is True
    assert "image" not in stored_source


def test_schema_validation_and_robot_cap(tmp_path):
    client, _, _ = make_client(tmp_path)

    unsupported = heartbeat()
    unsupported["schema_version"] = 3
    unsupported["boot_id"] = "019fce1c-a4b5-7ba2-b0d5-01ea4940926d"
    unsupported["sequence"] = 1
    unsupported["robots"][0]["runtime_kind"] = "physical"
    response = client.post("/api/v1/agents/heartbeat", json=unsupported)
    assert response.status_code == 400
    assert "unsupported schema_version" in response.json()["error"]

    malformed = heartbeat()
    del malformed["agent_id"]
    assert client.post("/api/v1/agents/heartbeat", json=malformed).status_code == 422

    inconsistent = heartbeat()
    inconsistent["robots"][0]["sources"][0]["fresh"] = False
    assert client.post("/api/v1/agents/heartbeat", json=inconsistent).status_code == 422

    oversized = heartbeat()
    template = oversized["robots"][0]
    oversized["robots"] = [
        {**template, "robot_id": f"robot-{index}"} for index in range(65)
    ]
    assert client.post("/api/v1/agents/heartbeat", json=oversized).status_code == 422


def test_registered_remote_agent_cannot_use_loopback_v1_ingress(tmp_path):
    client, _, _ = make_client(tmp_path)
    registered = client.post(
        "/api/v1/config/agent-registrations",
        json={"agent_id": "agent-1", "display_name": "Remote Agent"},
    )
    assert registered.status_code == 201

    response = client.post("/api/v1/agents/heartbeat", json=heartbeat())

    assert response.status_code == 403
    assert "mTLS ingress" in response.json()["error"]


def test_admin_api_rejects_non_loopback_host_header(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.get(
        "/api/v1/config/security",
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 421


def test_browser_security_headers_cover_ui_and_api(tmp_path):
    client, _, _ = make_client(tmp_path)

    page = client.get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in page.headers

    api = client.get("/api/v1/healthz")
    assert api.headers["cache-control"] == "no-store"


def test_summary_distinguishes_stale_fault_and_offline(tmp_path):
    client, store, clock = make_client(tmp_path)
    store.upsert(
        AgentHeartbeatV1.model_validate(heartbeat("online", state="RECORDING")),
        received_at=clock(),
    )
    store.upsert(
        AgentHeartbeatV1.model_validate(
            heartbeat("stale", fresh=False, fault="camera stale")
        ),
        received_at=clock() - timedelta(seconds=20),
    )
    store.upsert(
        AgentHeartbeatV1.model_validate(heartbeat("offline")),
        received_at=clock() - timedelta(seconds=45),
    )

    summary = client.get("/api/v1/summary").json()
    assert summary["agents"] == {
        "online": 1,
        "stale": 1,
        "offline": 1,
        "needs_attention": 2,
        "total": 3,
    }
    assert summary["robots"] == {
        "total": 3,
        "recording": 1,
        "faulted": 1,
        "not_fresh": 1,
        "low_rate": 0,
        "unreachable": 1,
    }
    assert summary["storage"]["disk_free_gb_min"] == 50.0


def test_multiple_agents_isolate_malformed_and_offline_updates(tmp_path):
    client, store, clock = make_client(tmp_path)
    assert client.post(
        "/api/v1/agents/heartbeat", json=heartbeat("agent-a")
    ).status_code == 202
    assert client.post(
        "/api/v1/agents/heartbeat", json=heartbeat("agent-b")
    ).status_code == 202

    clock.advance(FleetTTLs().offline_s + 1)
    assert client.post(
        "/api/v1/agents/heartbeat", json=heartbeat("agent-a")
    ).status_code == 202
    malformed = heartbeat("agent-b")
    malformed["robots"][0]["sources"][0]["topics"][0]["rate_ok"] = False
    assert client.post(
        "/api/v1/agents/heartbeat", json=malformed
    ).status_code == 422

    agents = {
        item["agent_id"]: item for item in client.get("/api/v1/agents").json()
    }
    assert agents["agent-a"]["status"] == "online"
    assert agents["agent-a"]["robots"][0]["sources_fresh"] is True
    assert agents["agent-b"]["status"] == "offline"
    assert store.get_snapshot("agent-b")["robots"][0]["rates_ok"] is True
    summary = client.get("/api/v1/summary").json()
    assert summary["agents"]["online"] == 1
    assert summary["agents"]["offline"] == 1


def test_route_surface_contains_no_robot_command_writer(tmp_path):
    client, _, _ = make_client(tmp_path)
    expanded_routes = []
    for route in client.app.routes:
        included = getattr(route, "original_router", None)
        expanded_routes.extend(included.routes if included is not None else [route])
    routes = {
        (method, route.path)
        for route in expanded_routes
        for method in getattr(route, "methods", set())
    }
    writers = {(method, path) for method, path in routes if method in {"POST", "PUT", "PATCH", "DELETE"}}
    assert writers == {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/users"),
        ("POST", "/api/v1/auth/service-identities"),
        ("PUT", "/api/v1/auth/principals/{principal_id}"),
        ("POST", "/api/v1/auth/service-tokens/{token_id}/revoke"),
        ("POST", "/api/v1/agents/heartbeat"),
        ("POST", "/api/v1/config/agent-registrations"),
        ("PUT", "/api/v1/config/agent-registrations/{agent_id}"),
        ("POST", "/api/v1/config/agent-credentials"),
        ("POST", "/api/v1/config/agent-credentials/{credential_id}/revoke"),
        ("POST", "/api/v1/config/connection-profiles"),
        ("PUT", "/api/v1/config/connection-profiles/{profile_id}"),
        ("POST", "/api/v1/config/processing-nodes"),
        ("PUT", "/api/v1/config/processing-nodes/{node_id}"),
        ("POST", "/api/v1/config/processing-nodes/{node_id}/decommission"),
        ("POST", "/api/v1/config/assets"),
        ("PUT", "/api/v1/config/assets/{asset_id}"),
        ("POST", "/api/v1/config/assets/{asset_id}/decommission"),
        ("PATCH", "/api/v1/config/assets/{asset_id}/runtime-kind"),
        ("POST", "/api/v1/config/workcells"),
        ("PUT", "/api/v1/config/workcells/{workcell_id}"),
        ("POST", "/api/v1/config/workcells/{workcell_id}/decommission"),
        ("POST", "/api/v1/config/task-definitions"),
        ("POST", "/api/v1/config/task-runs"),
        ("PUT", "/api/v1/config/task-runs/{run_id}"),
        ("POST", "/api/v1/config/task-runs/{run_id}/transition"),
        ("POST", "/api/v1/task-runs/{run_id}/complete-after-capture"),
        ("POST", "/api/v1/task-runs/{run_id}/capture-jobs"),
        ("POST", "/api/v1/capture-jobs/{job_id}/stop"),
        ("POST", "/api/v1/recording-sessions"),
        ("POST", "/api/v1/safety/assessments"),
        ("POST", "/api/v2/safety/evidence"),
        ("POST", "/api/v1/aas/projections"),
        ("POST", "/api/v1/commissioning/g6b/cases"),
        ("POST", "/api/v1/commissioning/g6b/artifacts"),
        ("POST", "/api/v1/commissioning/g6b/cases/{case_id}/submissions"),
        ("POST", "/api/v1/commissioning/g6b/submissions/{submission_id}/reviews"),
        ("POST", "/api/v1/orchestration/capability-profiles"),
        ("POST", "/api/v1/orchestration/device-manifests"),
        ("POST", "/api/v1/orchestration/workflows"),
        ("POST", "/api/v1/orchestration/safety-policies"),
        ("POST", "/api/v1/orchestration/runtime-bindings"),
        ("POST", "/api/v1/orchestration/coordinated-groups"),
        ("POST", "/api/v1/orchestration/zones"),
        ("POST", "/api/v1/orchestration/calibrations"),
        ("POST", "/api/v1/orchestration/control-envelopes"),
        ("POST", "/api/v1/orchestration/unity-build-attestations"),
        ("POST", "/api/v1/orchestration/registry/{kind}"),
        ("POST", "/api/v1/orchestration/runs"),
        ("POST", "/api/v1/orchestration/runs/{run_id}/transition"),
            ("POST", "/api/v1/orchestration/runs/{run_id}/producers"),
            ("POST", "/api/v1/workflow-authoring/drafts"),
            ("POST", "/api/v1/workflow-authoring/drafts/clone"),
            ("PUT", "/api/v1/workflow-authoring/drafts/{draft_id}"),
            ("POST", "/api/v1/workflow-authoring/drafts/{draft_id}/validate"),
            ("POST", "/api/v1/workflow-authoring/drafts/{draft_id}/publish"),
            ("POST", "/api/v1/workflow-authoring/action-library/drafts"),
            ("PUT", "/api/v1/workflow-authoring/action-library/drafts/{draft_id}"),
            (
                "POST",
                "/api/v1/workflow-authoring/action-library/drafts/{draft_id}/source-validation",
            ),
            (
                "POST",
                "/api/v1/workflow-authoring/action-library/drafts/{draft_id}/simulation-validation",
            ),
            (
                "POST",
                "/api/v1/workflow-authoring/action-library/drafts/{draft_id}/publish",
            ),
            ("POST", "/api/v1/workflow-authoring/simulation-profiles"),
            ("POST", "/api/v1/workflow-authoring/commissioning-candidates"),
            ("POST", "/api/v1/workflow-authoring/simulation-runs"),
            ("POST", "/api/v1/workflow-authoring/runtime/start"),
            ("POST", "/api/v1/workflow-authoring/runtime/stop"),
            (
                "POST",
                "/api/v1/workflow-authoring/simulation-runs/{run_id}/controls",
            ),
            ("POST", "/api/v1/control/role-bindings"),
        ("POST", "/api/v1/control/xr-sessions"),
        ("POST", "/api/v1/orchestration/runs/{run_id}/resume-challenges"),
        ("POST", "/api/v1/control/resume-challenges/{challenge_id}/approvals"),
        (
            "PUT",
            "/api/v1/recording-sessions/{session_id}/artifacts/{artifact_path:path}",
        ),
            ("POST", "/api/v1/recording-sessions/{session_id}/finalize"),
            (
                "POST",
                "/api/v1/recording-sessions/{session_id}/quality/analyze",
            ),
        }
    assert all(
        token not in path
        for _, path in routes
        for token in (
            "inference",
            "train",
            "robot-command",
            "policy/start",
            "weld/start",
            "feeder/start",
        )
    )


def test_event_api_records_stale_recovery_and_rate_transitions(tmp_path):
    client, _, clock = make_client(tmp_path)
    assert client.post("/api/v1/agents/heartbeat", json=heartbeat()).status_code == 202

    stale = heartbeat(
        state="DEGRADED",
        fresh=False,
        fault="stale required sources: observation.state",
    )
    clock.advance(1)
    assert client.post("/api/v1/agents/heartbeat", json=stale).status_code == 202

    low_rate = heartbeat(
        state="DEGRADED",
        rates_ok=False,
        rate_hz=10.0,
        fault="low-rate required sources: observation.state",
    )
    clock.advance(1)
    assert client.post("/api/v1/agents/heartbeat", json=low_rate).status_code == 202

    clock.advance(1)
    assert client.post("/api/v1/agents/heartbeat", json=heartbeat()).status_code == 202

    events = client.get("/api/v1/events", params={"agent_id": "agent-1"}).json()
    event_types = [event["event_type"] for event in reversed(events)]
    assert event_types == [
        "agent_online",
        "topic_stale",
        "topic_recovered",
        "topic_rate_low",
        "topic_rate_recovered",
    ]
    assert events[0]["details"]["rate_hz"] == 19.8

    before = len(events)
    clock.advance(1)
    assert client.post("/api/v1/agents/heartbeat", json=heartbeat()).status_code == 202
    assert len(client.get("/api/v1/events").json()) == before
