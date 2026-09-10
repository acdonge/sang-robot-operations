from datetime import datetime, timezone
import stat

from fastapi.testclient import TestClient
import pytest

from fleet_control_plane.app import create_app
from fleet_control_plane.auth import AuthManager
from fleet_control_plane.identity import main as identity_main
from fleet_control_plane.operations_models import AgentRegistrationCreate
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def authenticated_client(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    manager = AuthManager(store.path)
    manager.bootstrap_admin(
        principal_id="lab-admin",
        display_name="Lab Administrator",
        password="correct horse battery staple",
        occurred_at=NOW,
    )
    app = create_app(store, now=lambda: NOW, websocket_interval_s=0.01)
    return TestClient(app), manager


def login(client, principal_id, password):
    return client.post(
        "/api/v1/auth/login",
        json={"principal_id": principal_id, "password": password},
    )


def csrf_headers(client):
    return {"X-CSRF-Token": client.cookies.get("sang_csrf")}


def test_authentication_is_required_when_enabled(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    with pytest.raises(RuntimeError, match="fleet_identity bootstrap-admin"):
        create_app(store)

    client, _ = authenticated_client(tmp_path / "configured")
    assert client.get("/api/v1/healthz").status_code == 200
    assert client.get("/api/v1/agents").status_code == 401
    assert client.post("/api/v2/safety/evidence", json={}).status_code == 401
    assert login(client, "lab-admin", "wrong password value").status_code == 401

    signed_in = login(client, "lab-admin", "correct horse battery staple")
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["principal"]["role"] == "admin"
    assert client.get("/api/v1/auth/me").json()["principal"]["principal_id"] == "lab-admin"


def test_rbac_csrf_and_service_token_are_enforced(tmp_path):
    client, manager = authenticated_client(tmp_path)
    assert login(client, "lab-admin", "correct horse battery staple").status_code == 200

    body = {
        "principal_id": "observer",
        "display_name": "Observer",
        "role": "viewer",
        "password": "viewer password 1234",
    }
    assert client.post("/api/v1/auth/users", json=body).status_code == 403
    created = client.post(
        "/api/v1/auth/users",
        json=body,
        headers=csrf_headers(client),
    )
    assert created.status_code == 201, created.text

    service = client.post(
        "/api/v1/auth/service-identities",
        json={
            "principal_id": "catalog-reader",
            "display_name": "Catalog Reader",
            "role": "viewer",
            "token_id": "catalog-reader-token",
        },
        headers=csrf_headers(client),
    )
    assert service.status_code == 201, service.text
    token = service.json()["token"]
    assert token.startswith("sang_pat_")
    assert client.post(
        "/api/v1/config/agent-registrations",
        json={
            "agent_id": "processing-lab1",
            "display_name": "Processing Lab 1",
        },
        headers=csrf_headers(client),
    ).status_code == 201
    uploader = client.post(
        "/api/v1/auth/service-identities",
        json={
            "principal_id": "capture-uploader",
            "display_name": "Capture Uploader",
            "role": "operator",
            "token_id": "capture-uploader-token",
            "agent_ids": ["processing-lab1"],
        },
        headers=csrf_headers(client),
    )
    assert uploader.status_code == 201, uploader.text
    listed = client.get("/api/v1/auth/principals").json()
    assert {item["principal_id"] for item in listed} == {
        "lab-admin",
        "observer",
        "catalog-reader",
        "capture-uploader",
    }
    assert all("secret_hash" not in item and "token" not in item for item in listed)

    viewer = TestClient(client.app)
    assert login(viewer, "observer", "viewer password 1234").status_code == 200
    assert viewer.get("/api/v1/agents").status_code == 200
    denied = viewer.post(
        "/api/v1/config/processing-nodes",
        json={"node_id": "node-a", "display_name": "Node A"},
        headers=csrf_headers(viewer),
    )
    assert denied.status_code == 403
    assert viewer.post(
        "/api/v1/recording-sessions",
        json={},
        headers=csrf_headers(viewer),
    ).status_code == 403

    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/agents", headers=bearer).status_code == 200
    assert client.post(
        "/api/v1/config/processing-nodes",
        json={"node_id": "node-a", "display_name": "Node A"},
        headers=bearer,
    ).status_code == 403
    uploader_bearer = {"Authorization": f"Bearer {uploader.json()['token']}"}
    assert client.post(
        "/api/v1/recording-sessions",
        json={},
        headers=uploader_bearer,
    ).status_code == 422

    audit = manager.list_security_audit(limit=100)
    assert any(item["action"] == "login" and item["outcome"] == "success" for item in audit)
    assert any(item["action"] == "http_post" and item["outcome"] == "denied" for item in audit)


def test_authenticated_websocket_and_logout(tmp_path):
    client, _ = authenticated_client(tmp_path)
    assert login(client, "lab-admin", "correct horse battery staple").status_code == 200
    with client.websocket_connect("/api/v1/fleet") as websocket:
        assert websocket.receive_json()["type"] == "fleet"

    logged_out = client.post(
        "/api/v1/auth/logout",
        headers=csrf_headers(client),
    )
    assert logged_out.status_code == 200, logged_out.text
    assert client.get("/api/v1/agents").status_code == 401


def test_offline_service_identity_writes_private_one_time_token(tmp_path, capsys):
    store = FleetStore(tmp_path / "fleet.db")
    manager = AuthManager(store.path)
    manager.bootstrap_admin(
        principal_id="lab-admin",
        display_name="Lab Administrator",
        password="correct horse battery staple",
        occurred_at=NOW,
    )
    store.create_agent_registration(
        AgentRegistrationCreate(
            agent_id="processing-lab1",
            display_name="Processing Lab 1",
        ),
        occurred_at=NOW,
    )
    token_path = tmp_path / "secrets" / "edge-uploader.token"
    identity_main(
        [
            "--db-path",
            str(store.path),
            "create-service",
            "--actor-id",
            "lab-admin",
            "--principal-id",
            "edge-uploader",
            "--display-name",
            "Edge Uploader",
            "--role",
            "operator",
            "--token-id",
            "edge-uploader-01",
            "--agent-id",
            "processing-lab1",
            "--token-output",
            str(token_path),
        ]
    )
    token = token_path.read_text(encoding="utf-8").strip()
    assert token.startswith("sang_pat_")
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert token not in capsys.readouterr().out
    assert manager.authenticate_bearer(token, occurred_at=NOW).principal_id == "edge-uploader"
    with pytest.raises(FileExistsError):
        identity_main(
            [
                "--db-path",
                str(store.path),
                "create-service",
                "--actor-id",
                "lab-admin",
                "--principal-id",
                "other-uploader",
                "--display-name",
                "Other Uploader",
                "--role",
                "operator",
                "--token-id",
                "other-uploader-01",
                "--agent-id",
                "processing-lab1",
                "--token-output",
                str(token_path),
            ]
        )
