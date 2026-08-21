import pytest

from isaac_vendor.scene_runner import _require_ros_isolation, parse_args


def test_headless_ros_publish_requires_bounded_runtime():
    with pytest.raises(SystemExit):
        parse_args(["--profile", "omy", "--publish-ros"])


def test_headless_ros_publish_accepts_positive_runtime():
    options = parse_args(
        ["--profile", "rb5", "--publish-ros", "--run-seconds", "5"]
    )

    assert options.publish_ros is True
    assert options.run_seconds == 5.0


def test_ros_isolation_contract(monkeypatch):
    values = {
        "ROS_DISTRO": "jazzy",
        "ROS_DOMAIN_ID": "99",
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "SUBNET",
        "FASTDDS_BUILTIN_TRANSPORTS": "UDPv4",
        "RMW_FASTRTPS_PUBLICATION_MODE": "ASYNCHRONOUS",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    _require_ros_isolation()
    monkeypatch.setenv("ROS_DOMAIN_ID", "42")
    _require_ros_isolation()
    monkeypatch.setenv("ROS_DOMAIN_ID", "0")
    with pytest.raises(RuntimeError, match="ROS_DOMAIN_ID"):
        _require_ros_isolation()


def test_ros_isolation_rejects_non_numeric_domain(monkeypatch):
    values = {
        "ROS_DISTRO": "jazzy",
        "ROS_DOMAIN_ID": "simulation",
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "SUBNET",
        "FASTDDS_BUILTIN_TRANSPORTS": "UDPv4",
        "RMW_FASTRTPS_PUBLICATION_MODE": "ASYNCHRONOUS",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="ROS_DOMAIN_ID"):
        _require_ros_isolation()
