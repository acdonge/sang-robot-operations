"""Thread-safe latest-value buffers for ROS subscription callbacks."""

from dataclasses import dataclass
import math
import threading
import time
from typing import Any


@dataclass(frozen=True)
class BufferSnapshot:
    """An immutable view of one value received by a subscription."""

    msg: Any
    received_monotonic: float
    seq: int


class LatestValueBuffer:
    """Store only the most recently received message and its receipt metadata."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._msg: Any = None
        self._received_monotonic: float | None = None
        self._seq = 0

    def update(
        self, msg: Any, *, received_monotonic: float | None = None
    ) -> BufferSnapshot:
        """Replace the current value and return the resulting snapshot."""
        received_at = (
            time.monotonic() if received_monotonic is None else received_monotonic
        )
        if not isinstance(received_at, (int, float)) or not math.isfinite(received_at):
            raise ValueError("received_monotonic must be finite")

        with self._lock:
            self._seq += 1
            self._msg = msg
            self._received_monotonic = float(received_at)
            return BufferSnapshot(self._msg, self._received_monotonic, self._seq)

    def snapshot(self) -> BufferSnapshot | None:
        """Return an atomic snapshot, or ``None`` before the first message."""
        with self._lock:
            if self._received_monotonic is None:
                return None
            return BufferSnapshot(
                self._msg, self._received_monotonic, self._seq
            )

    def age_ms(self, *, now_monotonic: float | None = None) -> float:
        """Return age in milliseconds, or infinity before the first message."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now_monotonic must be finite")

        with self._lock:
            if self._received_monotonic is None:
                return math.inf
            return max(0.0, (float(now) - self._received_monotonic) * 1000.0)
