from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / "docker" / name).read_text(encoding="utf-8"))


def test_edge_ros_discovery_is_host_local_and_preview_is_not_advertised():
    services = _compose("compose.phase3-edge.yaml")["services"]

    for name in (
        "fake-so101",
        "arm-reader",
        "camera-reader",
        "agent",
        "opcua-gateway",
    ):
        assert services[name]["environment"][
            "ROS_AUTOMATIC_DISCOVERY_RANGE"
        ] == "LOCALHOST"
        assert services[name]["ipc"] == "host"
    assert "--suppress-preview-advertisement" in services["agent"]["command"]


def test_edge_physical_profile_runs_only_readonly_adapters_with_device_scopes():
    services = _compose("compose.phase3-edge.yaml")["services"]
    arm = services["arm-reader"]
    camera = services["camera-reader"]

    assert arm["profiles"] == ["physical"]
    assert camera["profiles"] == ["physical"]
    assert arm["command"][2] == "so101_readonly_adapter.arm_node"
    assert camera["command"][2] == "so101_readonly_adapter.camera_node"
    assert arm["device_cgroup_rules"] == ["c 166:* rmw"]
    assert camera["device_cgroup_rules"] == ["c 81:* rmw"]
    assert "/dev:/dev" in arm["volumes"]
    assert "/dev:/dev" in camera["volumes"]


def test_central_has_a_real_uninterrupted_final_gate_service():
    service = _compose("compose.phase3-central.yaml")["services"][
        "shadow-final-gate"
    ]

    assert service["profiles"] == ["final-validation"]
    assert "--final-gate" in service["command"]
    duration_index = service["command"].index("--duration-s") + 1
    assert service["command"][duration_index] == "${SHADOW_FINAL_DURATION_S:-7200}"
    assert service["command"][-1] == "/var/lib/sang/phase3-shadow-final.json"


def test_central_recording_ingress_is_a_separate_mtls_data_plane():
    service = _compose("compose.phase3-central.yaml")["services"][
        "recording-ingress"
    ]
    command = service["command"]

    assert service["profiles"] == ["recording"]
    assert service["network_mode"] == "host"
    assert service["restart"] == "unless-stopped"
    assert command[2] == "fleet_control_plane.recording_ingress"
    assert command[command.index("--port") + 1] == "${CENTRAL_RECORDING_PORT:-8444}"
    assert command[command.index("--object-store-root") + 1] == (
        "/var/lib/sang-objects"
    )
    assert "--client-ca-crl-bundle-ref" in command
    assert any(
        volume.endswith(":/var/lib/sang-objects") for volume in service["volumes"]
    )


def test_rb5_lab1_sync_probe_is_read_only_and_host_local():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"][
        "sync-probe"
    ]

    assert service["profiles"] == ["sync-probe"]
    assert service["network_mode"] == "host"
    assert service["ipc"] == "host"
    assert service["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
    assert service["command"][2] == "robot_agent.sync_probe"
    assert "--joint-topic" in service["command"]
    assert service.get("devices") is None
    assert any("d455_color=" in value for value in service["command"])
    assert any("d405_color=" in value for value in service["command"])
    assert any("welding_view=" in value for value in service["command"])

    d405 = _compose("compose.rb5-lab1-observation.yaml")["services"]["camera-d405"]
    assert d405["command"][-1] == "d405_color"
    assert d405["devices"] == [
        "${D405_COLOR_DEVICE:?set D405_COLOR_DEVICE}:/dev/camera-d405"
    ]


def test_rb5_lab1_clock_monitor_observes_without_adjusting_host_time():
    services = _compose("compose.rb5-lab1-observation.yaml")["services"]
    monitor = services["clock-monitor"]

    assert monitor["network_mode"] == "host"
    assert monitor["read_only"] is True
    assert "SYS_TIME" not in monitor.get("cap_add", [])
    assert "NET_BIND_SERVICE" in monitor["cap_add"]
    assert "-x" in monitor["command"]
    assert monitor.get("devices") is None
    assert services["agent"]["depends_on"]["clock-monitor"]["condition"] == (
        "service_healthy"
    )
    assert services["recording-worker"]["depends_on"]["clock-monitor"][
        "condition"
    ] == "service_healthy"


def test_rb5_lab1_capture_v2_records_registered_four_source_contract():
    profile = yaml.safe_load(
        (ROOT / "config" / "rb5_lab1_capture.yaml").read_text(encoding="utf-8")
    )
    topics = {topic["key"]: topic for topic in profile["topics"]}

    assert profile["profile_id"] == "rb5-lab1-observation-v2"
    assert set(topics) == {
        "rb5_joint",
        "d455_color",
        "d405_color",
        "welding_view",
    }
    assert topics["rb5_joint"]["asset_id"] == "rb5-lab1"
    assert topics["d455_color"]["asset_id"] == "rb5-lab1-d455"
    assert topics["d405_color"]["asset_id"] == "rb5-lab1-d405"
    assert topics["welding_view"]["asset_id"] == "rb5-lab1-otos-wgc200"
    assert all(topic["require_header_timestamp"] for topic in topics.values())


def test_rb5_network_address_recovers_after_dock_reconnect():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"]["rb5-net"]
    script = service["command"][-1]

    assert "ensure_address" in script
    assert 'ip address replace "$${RB5_HOST_CIDR}"' in script
    assert "while sleep 1" in script


def test_rb5_lab1_otos_preserves_resolution_and_uses_mjpg_passthrough():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"][
        "camera-otos"
    ]
    config = yaml.safe_load(
        (ROOT / "config" / "rb5_lab1_cameras.yaml").read_text(encoding="utf-8")
    )
    welding_view = next(
        camera for camera in config["cameras"] if camera["key"] == "welding_view"
    )

    assert service["profiles"] == ["otos"]
    assert welding_view["width"] == 1920
    assert welding_view["height"] == 1080
    assert welding_view["fps"] == 30
    assert welding_view["fourcc"] == "MJPG"
    assert welding_view["mjpg_passthrough"] is True


def test_rb5_lab1_manifest_advertises_otos_as_an_optional_camera():
    manifest = yaml.safe_load(
        (ROOT / "manifests" / "rb5_lab1_eval_01.yaml").read_text(
            encoding="utf-8"
        )
    )
    cameras = {
        camera["key"]: camera for camera in manifest["interfaces"]["cameras"]
    }

    assert cameras["welding_view"]["width"] == 1920
    assert cameras["welding_view"]["height"] == 1080
    assert cameras["welding_view"]["minimum_rate_hz"] == 25
    assert "welding_view" not in manifest["monitoring"]["required_sources"]
    assert manifest["ui"]["camera_order"][0] == "welding_view"


def test_rb5_lab1_agent_advertises_only_an_mtls_preview():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"]["agent"]
    command = service["command"]

    assert command[command.index("--preview-host") + 1] == "0.0.0.0"
    assert command[command.index("--preview-security-mode") + 1] == "mtls"
    assert command[command.index("--preview-public-base-url") + 1].startswith(
        "https://${EDGE_PREVIEW_PUBLIC_HOST:"
    )
    assert "--preview-tls-server-certificate-ref" in command
    assert "--preview-tls-server-private-key-ref" in command
    assert "--preview-tls-client-ca-crl-bundle-ref" in command
    assert "--preview-tls-authorized-client-sha256" in command
    assert "--suppress-preview-advertisement" not in command


def test_rb5_lab1_uploader_uses_readonly_capture_and_agent_mtls_identity():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"][
        "recording-uploader"
    ]
    command = service["command"]

    assert service["profiles"] == ["central"]
    assert service["restart"] == "unless-stopped"
    assert command[2] == "fleet_control_plane.recording_auto_upload"
    assert command[command.index("--central-url") + 1] == (
        "${CENTRAL_RECORDING_URL:?set CENTRAL_RECORDING_URL}"
    )
    assert command[command.index("--agent-id") + 1] == (
        "${SANG_AGENT_ID:?set SANG_AGENT_ID}"
    )
    assert command[command.index("--profile-id") + 1] == (
        "${CAPTURE_PROFILE_ID:-rb5-lab1-observation-v2}"
    )
    assert "--client-certificate" in command
    assert "--client-private-key" in command
    capture_mount = next(
        volume
        for volume in service["volumes"]
        if isinstance(volume, dict) and volume.get("target") == "/var/lib/sang/captures"
    )
    assert capture_mount["read_only"] is True


def test_rb5_lab1_recording_worker_is_fixed_profile_and_has_no_control_devices():
    service = _compose("compose.rb5-lab1-observation.yaml")["services"][
        "recording-worker"
    ]
    command = service["command"]

    assert service["profiles"] == ["central"]
    assert service["restart"] == "unless-stopped"
    assert service["network_mode"] == "host"
    assert service["ipc"] == "host"
    assert service.get("devices") is None
    assert service.get("privileged") is None
    assert service.get("cap_add") is None
    assert all("docker.sock" not in str(volume) for volume in service["volumes"])
    assert command[2] == "fleet_control_plane.recording_worker"
    assert command[command.index("--central-url") + 1] == (
        "${CENTRAL_INGRESS_URL:?set CENTRAL_INGRESS_URL}"
    )
    assert command[command.index("--profile") + 1] == (
        "/ws/config/rb5_lab1_capture.yaml"
    )
    assert command[command.index("--worker-id") + 1] == (
        "${CAPTURE_WORKER_ID:-processing-lab1-rb5-capture}"
    )
    assert not any(
        token in " ".join(command).lower()
        for token in ("robot command", "weld", "wire feeder", "ros2 service", "ros2 action")
    )


def test_phase3_lab1_central_preview_override_uses_mtls_client_material():
    service = _compose("compose.phase3-lab1-preview.yaml")["services"]["admin"]
    command = service["command"]

    assert any(
        volume.endswith(":/run/secrets/sang:ro") for volume in service["volumes"]
    )
    assert any(
        volume.endswith(":/var/lib/sang-objects") for volume in service["volumes"]
    )
    assert command[command.index("--object-store-root") + 1] == (
        "/var/lib/sang-objects"
    )
    assert command[command.index("--preview-secret-root") + 1] == "/run/secrets/sang"
    assert command[command.index("--preview-ca-bundle-ref") + 1] == (
        "secret://https/trust/ca-crl.pem"
    )
    assert command[command.index("--preview-client-certificate-ref") + 1] == (
        "secret://https/central/preview-client-chain.pem"
    )
    assert command[command.index("--preview-client-private-key-ref") + 1] == (
        "secret://https/central/preview-client-key.pem"
    )
