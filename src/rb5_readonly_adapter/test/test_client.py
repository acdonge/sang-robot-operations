import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from rb5_readonly_adapter.client import RB5DataClient
from rb5_readonly_adapter.node import parse_args


def test_client_reads_measured_angles_in_radians_from_data_port_only():
    calls = []

    class Channel:
        def request_data(self, timeout):
            calls.append(("request_data", timeout))
            return SimpleNamespace(
                sdata=SimpleNamespace(jnt_ang=[0, 90, -90, 180, -180, 45])
            )

    def factory(address, port):
        calls.append(("connect", address, port))
        return Channel()

    client = RB5DataClient("10.0.2.7", channel_factory=factory)

    positions = client.read_joint_positions(0.25)

    assert calls == [("connect", "10.0.2.7", 5001), ("request_data", 0.25)]
    assert positions == pytest.approx(
        [0, math.pi / 2, -math.pi / 2, math.pi, -math.pi, math.pi / 4]
    )


def test_client_rejects_timeout_and_invalid_joint_payloads():
    class TimeoutChannel:
        def request_data(self, _timeout):
            return None

    timeout_client = RB5DataClient(
        "10.0.2.7",
        channel_factory=lambda _address, _port: TimeoutChannel(),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        timeout_client.read_joint_positions(0.1)

    class ShortChannel:
        def request_data(self, _timeout):
            return SimpleNamespace(sdata=SimpleNamespace(jnt_ang=[0] * 5))

    short_client = RB5DataClient(
        "10.0.2.7",
        channel_factory=lambda _address, _port: ShortChannel(),
    )
    with pytest.raises(RuntimeError, match="expected 6"):
        short_client.read_joint_positions(0.1)


def test_adapter_source_excludes_vendor_command_channel_apis():
    source_root = Path(__file__).parents[1] / "rb5_readonly_adapter"
    source = "\n".join(path.read_text() for path in source_root.glob("*.py"))

    assert "CobotData" in source
    for forbidden in (
        "set_operation_mode",
        "set_speed_bar",
        "move_j",
        "move_l",
        "task_play",
        "task_stop",
        "Cobot(",
    ):
        assert forbidden not in source


def test_cli_defaults_match_rb5_manifest_contract():
    options = parse_args([])

    assert options.robot_ip == "10.0.2.7"
    assert options.data_port == 5001
    assert options.topic == "/rbpodo/joint_states"
    assert options.publish_hz == 30.0
