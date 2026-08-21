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
    assert not any("welding_view=" in value for value in service["command"])

    d405 = _compose("compose.rb5-lab1-observation.yaml")["services"]["camera-d405"]
    assert d405["command"][-1] == "d405_color"
    assert d405["devices"] == [
        "${D405_COLOR_DEVICE:?set D405_COLOR_DEVICE}:/dev/camera-d405"
    ]


def test_rb5_lab1_otos_preserves_resolution_and_uses_mjpg_passthrough():
    config = yaml.safe_load(
        (ROOT / "config" / "rb5_lab1_cameras.yaml").read_text(encoding="utf-8")
    )
    welding_view = next(
        camera for camera in config["cameras"] if camera["key"] == "welding_view"
    )

    assert welding_view["width"] == 1920
    assert welding_view["height"] == 1080
    assert welding_view["fps"] == 30
    assert welding_view["fourcc"] == "MJPG"
    assert welding_view["mjpg_passthrough"] is True
