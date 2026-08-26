from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path

import pytest

from robot_agent.preview import (
    CameraPreviewServer,
    HealthSnapshotStore,
    PreviewFrameStore,
)
from robot_agent.heartbeat import AgentTLSConfig
from robot_agent.node import build_preview_tls_server_context, parse_args
from sang_security.pki import initialize_pki, issue_certificate


JPEG = b"\xff\xd8test-frame\xff\xd9"


def test_frame_store_keeps_latest_and_waits_for_new_sequence():
    store = PreviewFrameStore()
    first = store.update("robot-1", "wrist", JPEG, received_monotonic=1.0)
    second = store.update("robot-1", "wrist", JPEG + b"next", received_monotonic=2.0)

    assert first.sequence == 1
    assert second.sequence == 2
    assert store.get("robot-1", "wrist") == second
    assert store.wait_for_newer("robot-1", "wrist", 1, timeout_s=0.01) == second
    assert store.wait_for_newer("robot-1", "wrist", 2, timeout_s=0.01) is None


def test_frame_store_rejects_non_jpeg():
    with pytest.raises(ValueError, match="not JPEG"):
        PreviewFrameStore().update("robot-1", "wrist", b"not-an-image")


def test_health_snapshot_store_rejects_sequence_rollback():
    store = HealthSnapshotStore()
    snapshot = {
        "robot_id": "robot-1",
        "boot_id": "boot-a",
        "sequence": 2,
    }
    store.update(snapshot)

    with pytest.raises(ValueError, match="did not advance"):
        store.update({**snapshot, "sequence": 1})


def test_preview_server_serves_health_snapshot_and_mjpeg():
    server = CameraPreviewServer("127.0.0.1", 0)
    server.frame_store.update("robot-1", "wrist cam", JPEG)
    health = {"robot_id": "robot-1", "boot_id": "boot-a", "sequence": 1}
    server.health_store.update(health)
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        request = Request(
            base + "/healthz",
            headers={"Origin": "http://127.0.0.1:18091"},
        )
        with urlopen(request, timeout=1) as response:
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == (
                "http://127.0.0.1:18091"
            )
            assert response.read() == b'{"status":"ok"}'
        denied_request = Request(
            base + "/healthz",
            headers={"Origin": "https://untrusted.example"},
        )
        with urlopen(denied_request, timeout=1) as response:
            assert response.headers["Access-Control-Allow-Origin"] is None
        with urlopen(
            base + "/robots/robot-1/cameras/wrist%20cam.jpg", timeout=1
        ) as response:
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.read() == JPEG
        with urlopen(server.health_snapshot_url("robot-1"), timeout=1) as response:
            assert response.headers["Content-Type"] == "application/json"
            assert response.read() == (
                b'{"boot_id":"boot-a","robot_id":"robot-1","sequence":1}'
            )
        stream_request = Request(
            server.stream_url("robot-1", "wrist cam"),
            headers={"Origin": "http://127.0.0.1:8090"},
        )
        with urlopen(stream_request, timeout=1) as response:
            assert response.headers["Content-Type"].startswith(
                "multipart/x-mixed-replace"
            )
            assert response.headers["Access-Control-Allow-Origin"] == (
                "http://127.0.0.1:8090"
            )
            chunk = response.read1(256)
            if JPEG not in chunk:
                chunk += response.read1(256)
            assert b"--frame" in chunk
            assert JPEG in chunk
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/robots/robot-1/cameras/missing.jpg", timeout=1)
        assert error.value.code == 404
    finally:
        server.stop()


def test_preview_mtls_requires_and_pins_central_client_certificate(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, common_name="Preview Test")
    server_material = issue_certificate(
        pki,
        issuer="https",
        name="preview-edge",
        role="https-server",
        ip_addresses=("127.0.0.1",),
    )
    client_material = issue_certificate(
        pki,
        issuer="https",
        name="central-preview",
        role="https-client",
        uri="urn:sang:central:preview-client",
    )
    bundle = pki / "bundles/https-ca-crl.pem"
    server_context = build_preview_tls_server_context(
        Path(server_material["chain"]),
        Path(server_material["private_key"]),
        bundle,
    )
    client_context = AgentTLSConfig(
        ca_bundle=bundle,
        client_certificate_chain=Path(client_material["chain"]),
        client_private_key=Path(client_material["private_key"]),
    ).context()
    server = CameraPreviewServer(
        "127.0.0.1",
        0,
        public_base_url="https://127.0.0.1:1",
        tls_context=server_context,
        authorized_client_sha256=frozenset(
            {client_material["certificate_sha256"]}
        ),
    )
    server.frame_store.update("robot-1", "wrist", JPEG)
    server.start()
    try:
        with urlopen(
            f"https://127.0.0.1:{server.port}/robots/robot-1/cameras/wrist.jpg",
            timeout=2,
            context=client_context,
        ) as response:
            assert response.read() == JPEG
    finally:
        server.stop()


def test_preview_mtls_cli_requires_authorized_client_pin():
    base = [
        "--manifest",
        "/tmp/robot.yaml",
        "--preview-host",
        "0.0.0.0",
        "--preview-port",
        "8443",
        "--preview-public-base-url",
        "https://edge.example:8443",
        "--preview-security-mode",
        "mtls",
        "--secret-root",
        "/tmp/secrets",
        "--preview-tls-server-certificate-ref",
        "secret://preview/server.pem",
        "--preview-tls-server-private-key-ref",
        "secret://preview/server-key.pem",
        "--preview-tls-client-ca-crl-bundle-ref",
        "secret://preview/ca-crl.pem",
    ]
    with pytest.raises(SystemExit):
        parse_args(base)
    options = parse_args(
        [
            *base,
            "--preview-tls-authorized-client-sha256",
            "a" * 64,
        ]
    )
    assert options.preview_tls_authorized_client_sha256 == ["a" * 64]
