"""Read-only access to the Rainbow Robotics data channel."""

from __future__ import annotations

import math
from typing import Callable, Protocol


JOINT_COUNT = 6
DEFAULT_DATA_PORT = 5001


class DataChannel(Protocol):
    def request_data(self, timeout: float): ...


DataChannelFactory = Callable[[str, int], DataChannel]


def make_vendor_data_channel(address: str, port: int) -> DataChannel:
    import rbpodo

    return rbpodo.CobotData(address, port)


class RB5DataClient:
    """Read measured joint angles without opening the command channel."""

    def __init__(
        self,
        address: str,
        *,
        port: int = DEFAULT_DATA_PORT,
        channel_factory: DataChannelFactory = make_vendor_data_channel,
    ) -> None:
        self._address = address
        self._port = port
        self._channel_factory = channel_factory
        self._channel: DataChannel | None = None

    def close(self) -> None:
        self._channel = None

    def read_joint_positions(self, timeout_s: float) -> tuple[float, ...]:
        if self._channel is None:
            self._channel = self._channel_factory(self._address, self._port)
        state = self._channel.request_data(timeout_s)
        if state is None:
            raise TimeoutError("RB5 data request timed out")
        values = tuple(float(value) for value in state.sdata.jnt_ang)
        if len(values) != JOINT_COUNT:
            raise RuntimeError(
                f"RB5 returned {len(values)} joint values; expected {JOINT_COUNT}"
            )
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError("RB5 returned a non-finite joint value")
        return tuple(math.radians(value) for value in values)
