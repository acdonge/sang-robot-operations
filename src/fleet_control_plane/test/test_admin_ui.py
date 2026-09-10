from pathlib import Path
import re

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.store import FleetStore


UI_ROOT = Path(__file__).resolve().parents[3] / "ui-admin"
OPERATOR_ROOT = Path(__file__).resolve().parents[3] / "ui-operator"
WORKSPACE = Path(__file__).resolve().parents[3]
ADMIN_REACT_ROOT = WORKSPACE / "frontend/admin"


def read_admin_react_sources():
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ADMIN_REACT_ROOT / "src").rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )


def read_react_source(*paths):
    return "\n".join(
        (ADMIN_REACT_ROOT / path).read_text(encoding="utf-8") for path in paths
    )


def asset_paths(html):
    return re.findall(r'(?:href|src)="(/antd-assets/[^"]+)"', html)


def test_all_react_entries_are_split_and_served(tmp_path):
    package = (ADMIN_REACT_ROOT / "package.json").read_text(encoding="utf-8")
    assert '"antd"' in package
    assert '"react"' in package

    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        operator_ui_dir=OPERATOR_ROOT,
        auth_enabled=False,
    )
    client = TestClient(app)
    entries = (
        (UI_ROOT / "index.html", "/"),
        (UI_ROOT / "actions.html", "/actions.html"),
        (UI_ROOT / "config.html", "/config.html"),
        (UI_ROOT / "catalog.html", "/catalog.html"),
        (UI_ROOT / "workflow.html", "/workflow.html"),
        (UI_ROOT / "login.html", "/login.html"),
        (OPERATOR_ROOT / "index.html", "/operator/"),
    )
    for entry, route in entries:
        html = entry.read_text(encoding="utf-8")
        paths = asset_paths(html)
        assert '<div id="root"></div>' in html
        assert paths
        assert "<style" not in html
        assert not re.search(r"<script(?![^>]+src=)[^>]*>", html)
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache", route
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache", path


def test_ui_revalidates_html_and_fixed_assets_including_operator(tmp_path):
    workflow_html = (UI_ROOT / "workflow.html").read_text(encoding="utf-8")
    workflow_entry = re.search(
        r'src="(/antd-assets/workflow-[A-Za-z0-9_-]+\.js)"', workflow_html
    )
    assert workflow_entry is not None
    app = create_app(
        FleetStore(tmp_path / "fleet.db"),
        ui_dir=UI_ROOT,
        operator_ui_dir=OPERATOR_ROOT,
        auth_enabled=False,
    )
    client = TestClient(app)
    for route in (
        "/", "/workflow.html", "/operator/", "/operator/index.html",
        workflow_entry.group(1), "/antd-assets/styles.css",
        "/antd-assets/operator.js",
    ):
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["cache-control"] == "no-cache", route
        etag = response.headers["etag"]
        for method in (client.get, client.head):
            unchanged = method(route, headers={"If-None-Match": etag})
            assert unchanged.status_code == 304, route
            assert unchanged.headers["cache-control"] == "no-cache", route
            assert unchanged.headers["etag"] == etag
            changed = method(route, headers={"If-None-Match": '"previous-deployment"'})
            assert changed.status_code == 200, route
            assert changed.headers["cache-control"] == "no-cache", route
            assert changed.headers["etag"] == etag
    assert client.get("/api/v1/healthz").headers["cache-control"] == "no-store"


def test_configuration_and_operator_apps_preserve_separate_contracts(tmp_path):
    config = (UI_ROOT / "config.html").read_text(encoding="utf-8")
    operator = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    shell_source = read_react_source("src/components/AppShell.tsx")
    config_source = read_react_source("src/ConfigApp.tsx")
    workflow_source = read_react_source("src/WorkflowStudioApp.tsx")
    operator_source = read_react_source("src/OperatorApp.tsx")

    assert '/antd-assets/config.js' in config
    assert '/antd-assets/operator.js' in operator
    assert '/api/v1/workflow-authoring/drafts' in workflow_source
    assert 'Mark active (metadata only)' in config_source
    assert '>Start run<' not in config_source
    assert 'href="/#fleet"' in shell_source
    assert 'href="/#robot"' in shell_source
    assert 'runs: "/api/v1/config/task-runs"' in config_source
    assert 'href={`/operator/#run/${encodeURIComponent(run.run_id)}`}' in config_source
    assert 'requestJson(node ? "PUT" : "POST"' in config_source
    assert '/transition`' in config_source
    assert '/decommission`' in config_source
    assert 'item.lifecycle_state !== "decommissioned"' in config_source
    assert 'getJson<Configuration>("/api/v1/configuration")' in operator_source
    assert '/api/v1/task-runs/${encodeURIComponent(runId)}/fleet' in operator_source
    assert '/api/v1/task-runs/${encodeURIComponent(runId)}/stream' in operator_source
    assert UI_ROOT != OPERATOR_ROOT


def test_data_catalog_is_read_only_and_served(tmp_path):
    index = (UI_ROOT / "catalog.html").read_text(encoding="utf-8")
    source = read_react_source("src/CatalogApp.tsx")

    assert '/antd-assets/catalog.js' in index
    assert 'getJson<Configuration>("/api/v1/configuration")' in source
    assert '`/api/v1/recording-sessions?${sessionParameters}`' in source
    assert '/api/v1/recording-sessions/${encoded}' in source
    assert '/playback/frame?${parameters}' in source
    assert '`/api/v1/dataset-derivations?${derivationParameters}`' in source
    assert "observation only · no action" in source
    assert "Promise.allSettled" in source
    assert "info.max_camera_views" in source
    assert "onChangeComplete={finishScrub}" in source
    assert "pendingFrameRequestRef.current =" in source
    assert "frameEpochRef.current += 1" in source
    assert "response.status !== 429" in source
    assert 'initialQuery.get("run_id")' in source
    assert 'initialQuery.get("session_id")' in source
    assert "requestJson" not in source
    assert not re.search(r'\bmethod\s*:\s*"(POST|PUT|PATCH|DELETE)"', source)

    app = create_app(
        FleetStore(tmp_path / "fleet.db"), ui_dir=UI_ROOT, auth_enabled=False
    )
    client = TestClient(app)
    assert client.get("/catalog.html").status_code == 200


def test_all_first_party_ui_copy_is_english():
    ui_files = [
        UI_ROOT / "index.html",
        UI_ROOT / "actions.html",
        UI_ROOT / "config.html",
        UI_ROOT / "catalog.html",
        UI_ROOT / "workflow.html",
        UI_ROOT / "login.html",
        OPERATOR_ROOT / "index.html",
    ]
    ui_files.extend(
        path
        for path in (ADMIN_REACT_ROOT / "src").rglob("*")
        if path.suffix in {".ts", ".tsx"}
    )

    for path in ui_files:
        content = path.read_text(encoding="utf-8")
        assert not re.search(r"[가-힣]", content), path

    html_files = (
        UI_ROOT / "index.html",
        UI_ROOT / "actions.html",
        UI_ROOT / "config.html",
        UI_ROOT / "catalog.html",
        UI_ROOT / "workflow.html",
        UI_ROOT / "login.html",
        OPERATOR_ROOT / "index.html",
    )
    for path in html_files:
        assert '<html lang="en">' in path.read_text(encoding="utf-8")


def test_data_catalog_runtime_includes_indexed_mcap_reader():
    setup = (WORKSPACE / "src/fleet_control_plane/setup.py").read_text(
        encoding="utf-8"
    )
    dockerfile = (WORKSPACE / "docker/Dockerfile").read_text(encoding="utf-8")

    assert '"mcap==1.4.0"' in setup
    assert "mcap==1.4.0" in dockerfile


def test_workflow_studio_supports_immutable_publish_clone_and_simulation_only():
    index = (UI_ROOT / "workflow.html").read_text(encoding="utf-8")
    source = read_react_source(
        "src/WorkflowStudioApp.tsx",
        "src/workflow-simulation-selection.ts",
        "src/workflow-main.tsx",
        "src/components/AppShell.tsx",
    )

    workflow_entry = re.search(
        r'src="(/antd-assets/workflow-[A-Za-z0-9_-]+\.js)"', index
    )
    assert workflow_entry is not None
    assert (UI_ROOT / workflow_entry.group(1).lstrip("/")).is_file()
    assert 'selectPublicationSimulation(' in source
    assert 'publicationSimulation(row)' in source
    assert 'queueSimulation(row, compatibility, profile)' in source
    assert 'profile.adapter_profile_sha256 === compatibility.profile_sha256' in source
    assert 'simulation_compatibility.find((item) => item.runnable)' not in source
    assert 'Publish immutable version' in source
    assert 'Copy as a new Task' in source
    assert 'Create the next version' in source
    assert '/api/v1/workflow-authoring/simulation-runs' in source
    assert '/api/v1/workflow-authoring/action-definitions' in source
    assert '/api/v1/workflow-authoring/skill-definitions' in source
    assert 'function ActionParameterFields' in source
    assert '<InputNumber' in source
    assert 'Only safe action inputs are editable' in source
    assert 'This screen cannot upload or execute arbitrary code.' in source
    assert 'Add robot action' in source
    assert 'label="Step ID"' not in source
    assert 'label="Depends on"' not in source
    assert 'cannot send physical robot' in source
    assert 'window.sessionStorage.setItem' in source
    assert 'crypto.subtle.digest("SHA-256"' in source
    assert 'requestId = `req-${crypto.randomUUID()}`' in source
    assert 'href="/workflow.html"' in source
    api_source = read_react_source("src/api.ts")
    assert "responseError(payload" in api_source
    for forbidden in (
        "/command",
        "ros2 topic pub",
        "ros2 action send_goal",
        "/policy/start",
        "/inference/",
    ):
        assert forbidden not in source.lower()
    assert 'bound_asset_id' not in source


def test_action_library_requires_source_and_simulation_gates_before_publish():
    index = (UI_ROOT / "actions.html").read_text(encoding="utf-8")
    source = read_react_source(
        "src/ActionLibraryApp.tsx",
        "src/actions-main.tsx",
        "src/components/AppShell.tsx",
    )

    assert '/antd-assets/actions.js' in index
    assert 'href="/actions.html"' in source
    assert '/api/v1/workflow-authoring/action-library' in source
    assert 'Choose one C# or Python Action file' in source
    assert 'Source Gate' in source
    assert 'Simulation Gate' in source
    assert 'Publish immutable version' in source
    assert 'Central stores the source by SHA-256 and never executes it.' in source
    assert 'physical_output_connected' not in source
    assert 'ros2 topic pub' not in source.lower()
    assert '/command' not in source.lower()


def test_operator_app_writes_only_capture_jobs_and_has_no_actuator_path():
    index = (OPERATOR_ROOT / "index.html").read_text(encoding="utf-8")
    script = read_react_source(
        "src/OperatorApp.tsx", "src/components/MjpegCameraPanel.tsx", "src/preview.ts"
    )
    combined = index + script

    assert '/antd-assets/operator.js' in index
    assert 'requestJson("POST"' in script
    assert "/api/v1/task-runs/${encodeURIComponent(runId)}/capture-jobs" in script
    assert "/api/v1/capture-jobs/${encodeURIComponent(currentJob.job_id)}/stop" in script
    assert "/api/v1/recording-sessions/${encodeURIComponent(sessionId)}" in script
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
    assert "1 stream max" in script
    assert 'headers: { Accept: "multipart/x-mixed-replace" }' in script


def test_admin_ui_has_no_robot_command_request():
    index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    source = read_react_source(
        "src/App.tsx",
        "src/fleet.ts",
        "src/types.ts",
        "src/components/FleetOverview.tsx",
        "src/components/RobotDetail.tsx",
        "src/components/MjpegCameraPanel.tsx",
        "src/preview.ts",
    )
    combined = index + source

    assert not re.search(r'\bmethod\s*:\s*"(POST|PUT|PATCH|DELETE)"', source)
    for endpoint in (
        "/record/",
        "/inference/",
        "/api/train",
        "/upload",
        "/command",
        "/policy/start",
    ):
        assert endpoint not in combined

    assert 'agents: "/api/v1/agents"' in source
    assert 'summary: "/api/v1/summary"' in source
    assert 'events: "/api/v1/events?limit=100"' in source
    assert 'fleetSocket: "/api/v1/fleet"' in source
    assert "summary?.agents.needs_attention" in source
    assert 'kind: "state" | "camera" | "action"' in source
    assert "freshness_ms: number" in source
    assert "age_ms: number | null" in source
    assert "rate_hz: number | null" in source
    assert "minimum_rate_hz: number | null" in source
    assert "rate_ok: boolean | null" in source
    assert "event_counts: Record<string, number>" in source
    assert "recordings.recent" in source
    assert 'dataIndex: "transport_messages_lost"' in source
    assert 'className="capture-progress"' in source
    assert "recording.current_instruction" in source
    assert "task_definition_id: string | null" in source


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


def test_workflow_adapter_profile_reaches_all_manager_deployments():
    central = (WORKSPACE / "docker/compose.phase3-central.yaml").read_text(
        encoding="utf-8"
    )
    lab_overlay = (WORKSPACE / "docker/compose.phase3-lab1-preview.yaml").read_text(
        encoding="utf-8"
    )
    manager = (WORKSPACE / "docker/compose.manager-secure.yaml").read_text(
        encoding="utf-8"
    )
    profile = "/ws/contracts/tasks/iitp-scenario1-unity-adapter.v1.json"
    skill_catalog = "/ws/contracts/tasks/iitp-scenario1-skill-catalog.v2.json"

    assert profile in central
    assert profile in lab_overlay
    assert profile in manager
    assert skill_catalog in central
    assert skill_catalog in lab_overlay
    assert skill_catalog in manager
    assert "unity-workflow-worker:" in central
    assert "profiles: [unity-workflow]" in central
    assert "fleet_control_plane.workflow_simulation_worker" in central
    assert "${SANG_UNITY_BRIDGE_ROOT:-/tmp/sang-unity-scenario1}" in central
    worker_block = central.split("  unity-workflow-worker:", 1)[1].split(
        "\n  agent-ingress:", 1
    )[0]
    assert "network_mode: host" not in worker_block


def test_admin_ui_has_robot_detail_navigation_and_on_demand_preview():
    source = read_admin_react_sources()
    preview = (ADMIN_REACT_ROOT / "src/preview.ts").read_text(encoding="utf-8")

    assert "function detailHash(agentId: string, robotId: string)" in source
    assert "href={detailHash(agent.agent_id, robot.robot_id)}" in source
    assert 'placeholder="Select a robot"' in source
    assert "useMjpegPreview(identity)" in source
    assert "new AbortController()" in preview
    assert 'headers: { Accept: "multipart/x-mixed-replace" }' in preview
    assert "source.preview_url" in preview
    assert "URL.createObjectURL" in preview
    assert "URL.revokeObjectURL" in preview
    assert "setInterval" not in preview
    assert "1 stream max" in source


def test_react_admin_ui_reconciles_live_updates_without_page_replacement():
    app = (ADMIN_REACT_ROOT / "src/App.tsx").read_text(encoding="utf-8")
    fleet = (ADMIN_REACT_ROOT / "src/fleet.ts").read_text(encoding="utf-8")
    overview = (
        ADMIN_REACT_ROOT / "src/components/FleetOverview.tsx"
    ).read_text(encoding="utf-8")

    assert "startTransition" in fleet
    assert "new WebSocket" in fleet
    assert "window.setInterval" in fleet
    assert "setSelectedAgentId" in app
    assert 'rowKey="agent_id"' in overview
    assert "session.session_id" in overview
    assert "window.location.reload" not in app + fleet + overview
