from datetime import datetime, timezone

import pytest

from pathlib import Path

from robot_agent.heartbeat import (
    AgentTLSConfig,
    build_heartbeat,
    require_control_plane_transport,
    require_loopback_http,
)
from robot_agent.node import parse_args


ROBOT = {
    "robot_id": "so101_01",
    "model": "bi_so101",
    "fsm_state": "READY",
    "phase": "MONITORING",
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
            "topics": [
                {
                    "topic": "/so101/joint_states",
                    "age_ms": 25.0,
                    "rate_hz": 20.0,
                    "minimum_rate_hz": 18.0,
                    "rate_ok": True,
                    "seq": 10,
                    "fresh": True,
                }
            ],
        }
    ],
    "fault": None,
    "recording": {
        "active": False,
        "episode_index": 0,
        "episodes_total": 0,
        "frames": 0,
        "elapsed_s": 0.0,
        "current_instruction": None,
    },
}


def test_heartbeat_matches_control_plane_contract():
    payload = build_heartbeat(
        agent_id="workcell-soarm-01",
        agent_name="SO-ARM Workcell",
        robots=[ROBOT],
        storage={"cpu_pct": 20.0, "ram_free_gb": 10.0, "disk_free_gb": 50.0},
        sent_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert payload["agent_id"] == "workcell-soarm-01"
    assert payload["robots"][0]["phase"] == "MONITORING"
    assert payload["robots"][0]["sources"][0]["topics"][0]["seq"] == 10
    assert payload["robots"][0]["rates_ok"] is True
    assert payload["datasets"]["count"] == 0
    assert payload["recordings"]["state"] == "disabled"


def test_heartbeat_includes_bounded_recording_inventory():
    recordings = {
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
    }
    payload = build_heartbeat(
        agent_id="processing-lab1",
        agent_name="Lab1",
        robots=[ROBOT],
        storage={},
        recordings=recordings,
    )

    assert payload["recordings"] == recordings


def test_secure_heartbeat_v2_has_boot_and_monotonic_sequence_contract():
    payload = build_heartbeat(
        agent_id="workcell-soarm-01",
        agent_name="SO-ARM Workcell",
        robots=[ROBOT],
        storage={"cpu_pct": 20.0, "ram_free_gb": 10.0, "disk_free_gb": 50.0},
        boot_id="019fce1c-a4b5-7ba2-b0d5-01ea4940926d",
        sequence=42,
    )

    assert payload["schema_version"] == 2
    assert payload["boot_id"] == "019fce1c-a4b5-7ba2-b0d5-01ea4940926d"
    assert payload["sequence"] == 42
    with pytest.raises(ValueError, match="together"):
        build_heartbeat(
            agent_id="workcell-soarm-01",
            agent_name="SO-ARM Workcell",
            robots=[ROBOT],
            storage={},
            boot_id=payload["boot_id"],
        )


def test_secure_heartbeat_v3_requires_runtime_kind_for_every_robot():
    boot_id = "019fce1c-a4b5-7ba2-b0d5-01ea4940926d"
    payload = build_heartbeat(
        agent_id="workcell-soarm-01",
        agent_name="SO-ARM Workcell",
        robots=[{**ROBOT, "runtime_kind": "physical"}],
        storage={},
        boot_id=boot_id,
        sequence=1,
        schema_version=3,
    )

    assert payload["schema_version"] == 3
    assert payload["robots"][0]["runtime_kind"] == "physical"
    with pytest.raises(ValueError, match="requires physical or simulation"):
        build_heartbeat(
            agent_id="workcell-soarm-01",
            agent_name="SO-ARM Workcell",
            robots=[ROBOT],
            storage={},
            boot_id=boot_id,
            sequence=2,
            schema_version=3,
        )
    with pytest.raises(ValueError, match="must not contain runtime_kind"):
        build_heartbeat(
            agent_id="workcell-soarm-01",
            agent_name="SO-ARM Workcell",
            robots=[{**ROBOT, "runtime_kind": "physical"}],
            storage={},
            boot_id=boot_id,
            sequence=2,
            schema_version=2,
        )


def test_remote_transport_is_rejected_until_secure():
    require_loopback_http("http://127.0.0.1:8090")
    with pytest.raises(ValueError, match="authentication and TLS"):
        require_loopback_http("http://192.168.1.10:8090")
    with pytest.raises(ValueError, match="loopback HTTP"):
        require_loopback_http("file:///tmp/fleet")
    marker = AgentTLSConfig(Path("ca"), Path("cert"), Path("key"))
    require_control_plane_transport("https://192.168.1.10:8443", marker)
    with pytest.raises(ValueError, match="requires HTTPS"):
        require_control_plane_transport("http://192.168.1.10:8090", None)


def test_agent_startup_grace_defaults_and_validation():
    options = parse_args(["--manifest", "/tmp/robot.yaml"])

    assert options.startup_grace_s == 3.0
    assert options.preview_host == "127.0.0.1"
    assert options.preview_port == 0
    assert options.capture_root is None
    assert options.suppress_preview_advertisement is False
    assert options.heartbeat_schema_version == 1
    assert options.mapping == []
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--manifest",
                "/tmp/robot.yaml",
                "--startup-grace-s",
                "-1",
            ]
        )

    distributed = parse_args(
        [
            "--manifest",
            "/tmp/robot.yaml",
            "--suppress-preview-advertisement",
        ]
    )
    assert distributed.suppress_preview_advertisement is True

    mapped = parse_args(
        [
            "--manifest",
            "/tmp/robot.yaml",
            "--mapping",
            "/tmp/robot.mapping.v2.yaml",
        ]
    )
    assert mapped.mapping == [Path("/tmp/robot.mapping.v2.yaml")]


def test_camera_preview_url_matches_heartbeat_contract():
    camera_source = {
        **ROBOT["sources"][0],
        "key": "top",
        "kind": "camera",
        "preview_url": "http://127.0.0.1:8101/robots/so101_01/cameras/top.mjpg",
    }
    robot = {**ROBOT, "sources": [camera_source]}
    payload = build_heartbeat(
        agent_id="workcell-soarm-01",
        agent_name="SO-ARM Workcell",
        robots=[robot],
        storage={"cpu_pct": 20.0, "ram_free_gb": 10.0, "disk_free_gb": 50.0},
    )

    assert payload["robots"][0]["sources"][0]["preview_url"].endswith(
        "/top.mjpg"
    )


def test_preview_listener_rejects_non_loopback_and_invalid_port():
    with pytest.raises(SystemExit):
        parse_args(
            ["--manifest", "/tmp/robot.yaml", "--preview-host", "0.0.0.0"]
        )
    with pytest.raises(SystemExit):
        parse_args(
            ["--manifest", "/tmp/robot.yaml", "--preview-port", "70000"]
        )


def test_secure_preview_requires_local_health_hmac_identity():
    common = [
        "--manifest",
        "/tmp/robot.yaml",
        "--heartbeat-security-mode",
        "mtls",
        "--control-plane-url",
        "https://central.sang.local:8443",
        "--secret-root",
        "/tmp/secrets",
        "--tls-ca-bundle-ref",
        "secret://https/trust/ca-crl.pem",
        "--tls-client-certificate-ref",
        "secret://https/client/chain.pem",
        "--tls-client-private-key-ref",
        "secret://https/client/key.pem",
        "--preview-port",
        "8101",
    ]
    with pytest.raises(SystemExit):
        parse_args(common)

    options = parse_args(
        [
            *common,
            "--local-health-key-ref",
            "secret://local/health-hmac.key",
            "--local-health-key-id",
            "workcell-soarm-01-health-01",
        ]
    )
    assert options.local_health_key_id == "workcell-soarm-01-health-01"
    assert options.heartbeat_schema_version == 2

    v3 = parse_args(
        [
            *common,
            "--local-health-key-ref",
            "secret://local/health-hmac.key",
            "--local-health-key-id",
            "workcell-soarm-01-health-01",
            "--heartbeat-schema-version",
            "3",
        ]
    )
    assert v3.heartbeat_schema_version == 3
