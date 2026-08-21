from pathlib import Path
import re

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.store import FleetStore


UI_ROOT = Path(__file__).resolve().parents[3] / "ui-admin"
OPERATOR_ROOT = Path(__file__).resolve().parents[3] / "ui-operator"


def test_admin_ui_is_split_and_served(tmp_path):
    index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/styles.css?v=20260806.2">' in index
    assert '<script type="module" src="/app.js?v=20260806.2"></script>' in index
    assert "<style" not in index
    assert not re.search(r"<script(?![^>]+src=)[^>]*>", index)

    app = create_app(FleetStore(tmp_path / "fleet.db"), ui_dir=UI_ROOT)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_configuration_and_operator_apps_are_separate_and_served(tmp_path):
    config = (UI_ROOT / "config.html").read_text(encoding="utf-8")
    config_script = (UI_ROOT / "config.js").read_text(encoding="utf-8")
    operator = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    operator_script = (OPERATOR_ROOT / "operator.js").read_text(encoding="utf-8")

    assert 'src="/config.js?v=20260803.1"' in config
    assert '"/api/v1/config/task-runs"' in config_script
    assert "link.href = `/operator/#run/" in config_script
    assert 'src="./operator.js?v=20260803.1"' in operator
    assert 'getJson("/api/v1/configuration")' in operator_script
    assert "/api/v1/task-runs/${encodeURIComponent(runId)}/fleet" in operator_script
    assert "/api/v1/task-runs/${encodeURIComponent(runId)}/stream" in operator_script

    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        operator_ui_dir=OPERATOR_ROOT,
    )
    client = TestClient(app)
    assert client.get("/config.html").status_code == 200
    assert client.get("/config.js").status_code == 200
    assert client.get("/operator/").status_code == 200
    assert client.get("/operator/operator.js").status_code == 200


def test_operator_app_has_no_control_or_configuration_writer():
    index = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    script = (OPERATOR_ROOT / "operator.js").read_text(encoding="utf-8")
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
        "/api/v1/config/assets",
    ):
        assert endpoint not in combined
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
