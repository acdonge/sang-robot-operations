from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import ssl
import threading

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8relay-frame\xff\xd9"


class FiniteMjpegHandler(BaseHTTPRequestHandler):
    expected_path = "/robots/robot-a/cameras/top.mjpg"

    def log_message(self, format, *args):
        del format, args

    def do_GET(self):
        if self.path != self.expected_path:
            self.send_error(404)
            return
        payload = (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(JPEG)).encode("ascii")
            + b"\r\n\r\n"
            + JPEG
            + b"\r\n--frame--\r\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RedirectTargetHandler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, format, *args):
        del format, args

    def do_GET(self):
        type(self).hits += 1
        self.send_error(500)


class RedirectingMjpegHandler(BaseHTTPRequestHandler):
    location = ""

    def log_message(self, format, *args):
        del format, args

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()


def heartbeat(preview_url):
    topic = {
        "topic": "/camera/top/compressed",
        "age_ms": 20.0,
        "rate_hz": 30.0,
        "minimum_rate_hz": 25.0,
        "rate_ok": True,
        "seq": 10,
        "fresh": True,
    }
    return {
        "schema_version": 1,
        "agent_id": "agent-a",
        "agent_name": "Agent A",
        "sw_version": "0.1.0",
        "sent_at": NOW.isoformat(),
        "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 10},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "sync": {"supported": False, "state": "unsupported", "pending": None},
        "robots": [
            {
                "robot_id": "robot-a",
                "model": "test",
                "fsm_state": "READY",
                "phase": "monitoring",
                "sources_fresh": True,
                "rates_ok": True,
                "sources": [
                    {
                        "key": "top",
                        "kind": "camera",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "preview_url": preview_url,
                        "topics": [topic],
                    }
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


def configure_camera(client):
    assert client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "node-a",
            "display_name": "Node A",
            "agent_ids": ["agent-a"],
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/assets",
        json={
            "asset_id": "camera-top",
            "asset_type": "camera",
            "display_name": "Top Camera",
            "node_id": "node-a",
            "binding": {
                "agent_id": "agent-a",
                "robot_id": "robot-a",
                "source_key": "top",
            },
            "capabilities": ["preview"],
        },
    ).status_code == 201


def test_selected_camera_is_relayed_without_exposing_upstream_url(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FiniteMjpegHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        raw_url = (
            f"http://127.0.0.1:{upstream.server_address[1]}"
            "/robots/robot-a/cameras/top.mjpg"
        )
        store = FleetStore(tmp_path / "fleet.db")
        client = TestClient(create_app(store, now=lambda: NOW, auth_enabled=False))
        assert client.post("/api/v1/agents/heartbeat", json=heartbeat(raw_url)).status_code == 202

        unregistered = client.get("/api/v1/agents").json()[0]["robots"][0]["sources"][0]
        assert unregistered["preview_url"] is None
        assert unregistered["upstream_preview_available"] is True
        configure_camera(client)

        source = client.get("/api/v1/agents").json()[0]["robots"][0]["sources"][0]
        assert source["preview_url"] == "/api/v1/cameras/camera-top/preview.mjpg"
        assert raw_url not in str(source)
        relayed = client.get(source["preview_url"])
        assert relayed.status_code == 200, relayed.text
        assert relayed.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert JPEG in relayed.content
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def test_relay_rejects_noncanonical_or_remote_plain_http(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    client = TestClient(create_app(store, now=lambda: NOW, auth_enabled=False))
    wrong_path = "http://127.0.0.1:12345/arbitrary"
    assert client.post("/api/v1/agents/heartbeat", json=heartbeat(wrong_path)).status_code == 202
    configure_camera(client)
    assert client.get("/api/v1/cameras/camera-top/preview.mjpg").status_code == 502

    store.upsert(
        AgentHeartbeatV1.model_validate(
            heartbeat("http://192.0.2.30:8444/robots/robot-a/cameras/top.mjpg")
        ),
        received_at=NOW,
    )
    response = client.get("/api/v1/cameras/camera-top/preview.mjpg")
    assert response.status_code == 502
    assert "requires HTTPS" in response.json()["error"]

    store.upsert(
        AgentHeartbeatV1.model_validate(
            heartbeat("https://192.0.2.30:8444/robots/robot-a/cameras/top.mjpg")
        ),
        received_at=NOW,
    )
    response = client.get("/api/v1/cameras/camera-top/preview.mjpg")
    assert response.status_code == 503
    assert "preview TLS is not configured" in response.json()["error"]

    secure_client = TestClient(
        create_app(
            store,
            now=lambda: NOW,
            auth_enabled=False,
            preview_ssl_context=ssl.create_default_context(),
        )
    )
    response = secure_client.get("/api/v1/cameras/camera-top/preview.mjpg")
    assert response.status_code == 502
    assert "allowlisted mTLS peer" in response.json()["error"]


def test_relay_rejects_upstream_redirect_without_contacting_target(tmp_path):
    target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectingMjpegHandler)
    RedirectTargetHandler.hits = 0
    RedirectingMjpegHandler.location = f"http://127.0.0.1:{target.server_port}/internal"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        upstream_url = (
            f"http://127.0.0.1:{redirect.server_port}"
            "/robots/robot-a/cameras/top.mjpg"
        )
        store = FleetStore(tmp_path / "fleet.db")
        client = TestClient(create_app(store, now=lambda: NOW, auth_enabled=False))
        assert client.post(
            "/api/v1/agents/heartbeat",
            json=heartbeat(upstream_url),
        ).status_code == 202
        configure_camera(client)
        response = client.get("/api/v1/cameras/camera-top/preview.mjpg")
        assert response.status_code == 502
        assert "redirects are disabled" in response.json()["error"]
        assert RedirectTargetHandler.hits == 0
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_relay_releases_slot_even_if_body_is_never_iterated(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FiniteMjpegHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        raw_url = (
            f"http://127.0.0.1:{upstream.server_port}"
            "/robots/robot-a/cameras/top.mjpg"
        )
        store = FleetStore(tmp_path / "fleet.db")
        app = create_app(store, now=lambda: NOW, auth_enabled=False)
        client = TestClient(app)
        assert client.post("/api/v1/agents/heartbeat", json=heartbeat(raw_url)).status_code == 202
        configure_camera(client)
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "name", None) == "camera_preview"
        )

        first = endpoint("camera-top")
        assert first.background is not None
        asyncio.run(first.background())
        second = endpoint("camera-top")
        asyncio.run(second.background())
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
