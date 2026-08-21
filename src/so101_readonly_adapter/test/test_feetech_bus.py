from types import SimpleNamespace

import pytest

from so101_readonly_adapter.calibration import MOTOR_ORDER, parse_calibration
from so101_readonly_adapter.feetech_bus import ReadOnlyFeetechBus


def calibration():
    return parse_calibration(
        {
            name: {
                "id": index,
                "drive_mode": 0,
                "homing_offset": 0,
                "range_min": 0,
                "range_max": 4095,
            }
            for index, name in enumerate(MOTOR_ORDER, start=1)
        }
    )


def fake_sdk(*, model=777):
    operations = []

    class Port:
        def __init__(self, path):
            self.path = path
            self.is_open = False
            self.tx_time_per_byte = 0.01
            operations.append(("port", path))

        def getCurrentTime(self):
            return 100.0

        def setBaudRate(self, baudrate):
            operations.append(("set_baudrate", baudrate))
            self.is_open = True
            return True

        def closePort(self):
            operations.append(("close", self.path))
            self.is_open = False

    class Packet:
        def __init__(self, protocol):
            operations.append(("packet", protocol))

        def ping(self, _port, motor_id):
            operations.append(("ping", motor_id))
            return model, 0, 0

        def getTxRxResult(self, result):
            return f"comm {result}"

        def getRxPacketError(self, error):
            return f"error {error}"

    class Reader:
        def __init__(self, _port, _packet, address, length):
            self.ids = []
            operations.append(("reader", address, length))

        def addParam(self, motor_id):
            self.ids.append(motor_id)
            operations.append(("add_read_id", motor_id))
            return True

        def txRxPacket(self):
            operations.append(("sync_read", tuple(self.ids)))
            return 0

        def isAvailable(self, motor_id, _address, _length):
            return motor_id in self.ids

        def getData(self, motor_id, _address, _length):
            return 1000 + motor_id

    sdk = SimpleNamespace(
        COMM_SUCCESS=0,
        PortHandler=Port,
        PacketHandler=Packet,
        GroupSyncRead=Reader,
    )
    return sdk, operations


def test_transport_only_performs_handshake_and_reads():
    sdk, operations = fake_sdk()
    bus = ReadOnlyFeetechBus("/dev/test", calibration(), sdk=sdk)

    bus.open()
    positions = bus.read_positions()
    bus.close()

    assert positions == {motor_id: 1000 + motor_id for motor_id in range(1, 7)}
    assert [operation[0] for operation in operations] == [
        "port",
        "set_baudrate",
        "packet",
        "ping",
        "ping",
        "ping",
        "ping",
        "ping",
        "ping",
        "reader",
        "add_read_id",
        "add_read_id",
        "add_read_id",
        "add_read_id",
        "add_read_id",
        "add_read_id",
        "sync_read",
        "close",
    ]
    assert not hasattr(bus, "write")
    assert not hasattr(bus, "enable_torque")
    assert not hasattr(bus, "disable_torque")


def test_wrong_motor_model_closes_port_without_continuing():
    sdk, operations = fake_sdk(model=123)
    bus = ReadOnlyFeetechBus("/dev/test", calibration(), sdk=sdk)

    with pytest.raises(ConnectionError, match="expected 777"):
        bus.open()

    assert operations[-1] == ("close", "/dev/test")
    assert not bus.is_open
