from pathlib import Path
import re

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.store import FleetStore


UI_ROOT = Path(__file__).resolve().parents[3] / "ui-admin"
OPERATOR_ROOT = Path(__file__).resolve().parents[3] / "ui-operator"
WORKSPACE = Path(__file__).resolve().parents[3]


def test_admin_ui_is_split_and_served(tmp_path):
    index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/styles.css?v=20260806.2">' in index
    assert '<script type="module" src="/app.js?v=20260806.2"></script>' in index
    assert "<style" not in index
    assert not re.search(r"<script(?![^>]+src=)[^>]*>", index)

    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        auth_enabled=False,
    )
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_configuration_and_operator_apps_are_separate_and_served(tmp_path):
    config = (UI_ROOT / "config.html").read_text(encoding="utf-8")
    config_script = (UI_ROOT / "config.js").read_text(encoding="utf-8")
    operator = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    operator_script = (OPERATOR_ROOT / "operator.js").read_text(encoding="utf-8")

    assert 'src="/config.js?v=20260821.2"' in config
    assert '"/api/v1/config/task-runs"' in config_script
    assert "link.href = `/operator/#run/" in config_script
    assert 'editing ? "PUT" : "POST"' in config_script
    assert "/transition`" in config_script
    assert "/decommission`" in config_script
    assert 'item.lifecycle_state !== "decommissioned"' in config_script
    assert 'item.lifecycle_state !== "DECOMMISSIONED"' not in config_script
    assert 'src="./operator.js?v=20260826.1"' in operator
    assert 'getJson("/api/v1/configuration")' in operator_script
    assert "/api/v1/task-runs/${encodeURIComponent(runId)}/fleet" in operator_script
    assert "/api/v1/task-runs/${encodeURIComponent(runId)}/stream" in operator_script

    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        operator_ui_dir=OPERATOR_ROOT,
        auth_enabled=False,
    )
    client = TestClient(app)
    assert client.get("/config.html").status_code == 200
    assert client.get("/config.js").status_code == 200
    assert client.get("/operator/").status_code == 200
    assert client.get("/operator/operator.js").status_code == 200


def test_data_catalog_is_read_only_and_served(tmp_path):
    index = (UI_ROOT / "catalog.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "catalog.js").read_text(encoding="utf-8")

    assert 'href="/catalog.css?v=20260826.1"' in index
    assert 'src="/catalog.js?v=20260826.1"' in index
    assert 'id="playback-panel"' in index
    assert 'id="playback-camera-picker"' in index
    assert 'id="playback-timeline"' in index
    assert 'id="playback-toggle"' in index
    assert 'id="derivation-list"' in index
    assert 'getJson("/api/v1/configuration")' in script
    assert "/api/v1/recording-sessions?" in script
    assert "/api/v1/recording-sessions/${encodeURIComponent(sessionId)}" in script
    assert "/playback/frame?${parameters}" in script
    assert "/api/v1/dataset-derivations?${parameters}" in script
    assert "observation only · no action" in script
    assert "Promise.allSettled" in script
    assert "state.playback.info.max_camera_views" in script
    assert "const offsetMs = Number(event.currentTarget.value);" in script
    assert 'initialQuery.get("run_id")' in script
    assert 'initialQuery.get("session_id")' in script
    assert "method:" not in script
    assert not re.search(r"\b(POST|PUT|PATCH|DELETE)\b", script)

    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        auth_enabled=False,
    )
    client = TestClient(app)
    assert client.get("/catalog.html").status_code == 200
    assert client.get("/catalog.css").status_code == 200
    assert client.get("/catalog.js").status_code == 200


def test_data_catalog_runtime_includes_indexed_mcap_reader():
    setup = (WORKSPACE / "src/fleet_control_plane/setup.py").read_text(
        encoding="utf-8"
    )
    dockerfile = (WORKSPACE / "docker/Dockerfile").read_text(encoding="utf-8")

    assert '"mcap==1.4.0"' in setup
    assert "mcap==1.4.0" in dockerfile


def test_operator_app_writes_only_capture_jobs_and_has_no_actuator_path():
    index = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    script = (OPERATOR_ROOT / "operator.js").read_text(encoding="utf-8")
    combined = index + script

    assert 'id="capture-form"' in index
    assert 'id="capture-start"' in index
    assert 'id="capture-stop"' in index
    assert 'id="capture-hash"' in index
    assert 'id="capture-quality-state"' in index
    assert 'id="run-complete"' in index
    assert 'requestJson("POST"' in script
    assert "/api/v1/task-runs/${encodeURIComponent(state.runId)}/capture-jobs" in script
    assert "/api/v1/capture-jobs/${encodeURIComponent(job.job_id)}/stop" in script
    assert "/api/v1/recording-sessions/${encodeURIComponent(job.session_id)}" in script
    assert "/complete-after-capture" in script
    assert 'qualityState === "PASS"' in script
    assert "sessionStorage.setItem" in script
    for endpoint in (
        "/record/",
        "/inference/",
        "/api/train",
        "/command",
        "/policy/start",
        "/api/v1/config/assets",
        "/api/v1/config/task-runs",
    ):
        assert endpoint not in combined
    for command in ("ros2 topic pub", "ros2 service call", "ros2 action send_goal"):
        assert command not in combined.lower()
    assert "1 STREAM MAX" in index
    assert 'headers: { Accept: "multipart/x-mixed-replace" }' in script


def test_admin_ui_has_no_robot_command_request():
    index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    combined = index + script

    assert "method:" not in script
    assert not re.search(r"\b(POST|PUT|PATCH|DELETE)\b", script)
    for endpoint in (
        "/record/",
        "/inference/",
        "/api/train",
        "/upload",
        "/command",
        "/policy/start",
    ):
        assert endpoint not in combined

    assert 'agents: "/api/v1/agents"' in script
    assert 'summary: "/api/v1/summary"' in script
    assert 'events: "/api/v1/events?limit=100"' in script
    assert 'fleetSocket: "/api/v1/fleet"' in script
    assert 'summary.agents.needs_attention' in script
    assert 'source.kind' in script
    assert 'source.freshness_ms' in script
    assert 'topic.age_ms' in script
    assert 'topic.rate_hz' in script
    assert 'topic.minimum_rate_hz' in script
    assert 'topic.rate_ok' in script
    assert 'topic.seq' in script
    assert 'agent.event_counts' in script
    assert 'healthHistory(agent)' in script
    assert 'summary.recordings' in script
    assert 'recordingInventoryContent(agent)' in script
    assert 'session.transport_messages_lost' in script
    assert 'session.run_id' in script
    assert 'session.task_definition_id' in script


def test_operations_v1_compose_keeps_auth_and_read_only_opcua_boundary():
    compose = (WORKSPACE / "docker/compose.operations-v1.yaml").read_text(
        encoding="utf-8"
    )
    assert "--auth-disabled" not in compose
    assert "--object-store-root" in compose
    assert "fleet_control_plane.agent_ingress" in compose
    assert "${CENTRAL_INGRESS_PORT:-8443}" in compose
    assert "condition: service_healthy" in compose
    assert "fleet_control_plane.central_opcua" in compose
    assert "--security-mode\n      - sign-and-encrypt" in compose
    for forbidden in ("/command", "ros2 topic pub", "move_j", "set_writable"):
        assert forbidden not in compose


def test_remote_manager_is_https_authenticated_and_separate_from_local_http():
    compose = (WORKSPACE / "docker/compose.manager-secure.yaml").read_text(
        encoding="utf-8"
    )

    assert "--auth-disabled" not in compose
    assert "--https-server-certificate-ref" in compose
    assert "--https-server-private-key-ref" in compose
    assert "${MANAGER_HTTPS_PORT:-18443}" in compose
    assert "${MANAGER_PUBLIC_HOST:-10.0.0.8}" in compose
    assert "--host\n      - 0.0.0.0" in compose


def test_admin_ui_has_robot_detail_navigation_and_on_demand_preview():
    index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="robot-detail-nav"' in index
    assert 'id="robot-detail"' in index
    assert 'id="robot-selector"' in index
    assert "function detailHash(agentId, robotId)" in script
    assert 'sourceState.href = detailHash(agent.agent_id, robot.robot_id)' in script
    assert 'previewButton.dataset.action = "start-preview"' in script
    assert 'previewButton.dataset.action = "stop-preview"' in script
    assert 'image.id = "active-camera-preview"' in script
    assert 'image.src = "data:image/gif;base64' in script
    assert 'source.preview_url' in script
    assert "new AbortController()" in script
    assert "streamCameraPreview(source.preview_url" in script
    assert "setInterval" not in script.split("function cameraDossier", 1)[1].split(
        "function telemetryDossier", 1
    )[0]


def test_admin_ui_reconciles_live_updates_without_replacing_agent_views():
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function reconcileChildren(parent, nextChildren)" in script
    assert "row.dataset.renderKey" in script
    assert "content.dataset.renderKey" in script
    assert "image.dataset.renderKey" in script
    assert "reconcileChildren(list, agents.map(agentRow))" in script
    assert "reconcileChildren(inspector, [content])" in script
    assert "list.replaceChildren" not in script
    assert "inspector.replaceChildren" not in script
