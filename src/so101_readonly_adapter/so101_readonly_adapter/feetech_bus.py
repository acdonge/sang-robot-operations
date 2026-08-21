"""A deliberately write-incapable Feetech transport for SO-101 observations."""

from __future__ import annotations

from typing import Any

from so101_readonly_adapter.calibration import ArmCalibration


PROTOCOL_VERSION = 0
PRESENT_POSITION_ADDRESS = 56
PRESENT_POSITION_LENGTH = 2


class ReadOnlyFeetechBus:
    """Expose only handshake, present-position reads, and port close."""

    def __init__(
        self,
        port: str,
        calibration: ArmCalibration,
        *,
        baudrate: int = 1_000_000,
        expected_model: int = 777,
        sdk: Any | None = None,
    ) -> None:
        if sdk is None:
            import scservo_sdk as sdk_module

            sdk = sdk_module
        self.port = port
        self.calibration = calibration
        self.baudrate = baudrate
        self.expected_model = expected_model
        self._sdk = sdk
        self._port_handler = None
        self._packet_handler = None
        self._reader = None

    @property
    def is_open(self) -> bool:
        return self._port_handler is not None

    @staticmethod
    def _fix_packet_timeout(port_handler: Any) -> None:
        # feetech-servo-sdk 1.0.0 underestimates USB serial response time.
        def set_packet_timeout(packet_length: int) -> None:
            port_handler.packet_start_time = port_handler.getCurrentTime()
            port_handler.packet_timeout = (
                port_handler.tx_time_per_byte * packet_length
                + port_handler.tx_time_per_byte * 3.0
                + 50.0
            )

        port_handler.setPacketTimeout = set_packet_timeout

    def open(self) -> None:
        if self.is_open:
            return
        port_handler = self._sdk.PortHandler(self.port)
        self._fix_packet_timeout(port_handler)
        try:
            if not port_handler.setBaudRate(self.baudrate):
                raise ConnectionError(
                    f"failed to open {self.port} at {self.baudrate} baud"
                )
            packet_handler = self._sdk.PacketHandler(PROTOCOL_VERSION)
            for motor_id in self.calibration.motor_ids:
                model, result, error = packet_handler.ping(port_handler, motor_id)
                if result != self._sdk.COMM_SUCCESS:
                    detail = packet_handler.getTxRxResult(result)
                    raise ConnectionError(
                        f"motor {motor_id} did not answer on {self.port}: {detail}"
                    )
                if error:
                    detail = packet_handler.getRxPacketError(error)
                    raise ConnectionError(
                        f"motor {motor_id} reported an error on {self.port}: {detail}"
                    )
                if model != self.expected_model:
                    raise ConnectionError(
                        f"motor {motor_id} on {self.port} has model {model}; "
                        f"expected {self.expected_model}"
                    )
            reader = self._sdk.GroupSyncRead(
                port_handler,
                packet_handler,
                PRESENT_POSITION_ADDRESS,
                PRESENT_POSITION_LENGTH,
            )
            for motor_id in self.calibration.motor_ids:
                if not reader.addParam(motor_id):
                    raise ConnectionError(
                        f"failed to register motor {motor_id} for position reads"
                    )
        except Exception:
            if getattr(port_handler, "is_open", False):
                port_handler.closePort()
            raise
        self._port_handler = port_handler
        self._packet_handler = packet_handler
        self._reader = reader

    def read_positions(self) -> dict[int, int]:
        if not self.is_open or self._reader is None or self._packet_handler is None:
            raise RuntimeError(f"read-only bus {self.port} is not open")
        result = self._reader.txRxPacket()
        if result != self._sdk.COMM_SUCCESS:
            detail = self._packet_handler.getTxRxResult(result)
            raise ConnectionError(f"position read failed on {self.port}: {detail}")
        positions = {}
        for motor_id in self.calibration.motor_ids:
            if not self._reader.isAvailable(
                motor_id,
                PRESENT_POSITION_ADDRESS,
                PRESENT_POSITION_LENGTH,
            ):
                raise ConnectionError(
                    f"position response for motor {motor_id} is unavailable on {self.port}"
                )
            positions[motor_id] = int(
                self._reader.getData(
                    motor_id,
                    PRESENT_POSITION_ADDRESS,
                    PRESENT_POSITION_LENGTH,
                )
            )
        return positions

    def close(self) -> None:
        port_handler = self._port_handler
        self._reader = None
        self._packet_handler = None
        self._port_handler = None
        if port_handler is not None and getattr(port_handler, "is_open", False):
            port_handler.closePort()

    def __enter__(self) -> "ReadOnlyFeetechBus":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
