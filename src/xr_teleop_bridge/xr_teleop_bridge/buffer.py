"""Thread-safe latest-frame handoff between transport and ROS callbacks."""

from dataclasses import dataclass
import math
import threading
import time

from xr_teleop_bridge.protocol import XRFrameData


@dataclass(frozen=True)
class FrameSnapshot:
    frame: XRFrameData
    received_monotonic: float
    generation: int


class LatestFrameBuffer:
    """Keep only the freshest unpublished XR frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: FrameSnapshot | None = None
        self._pending: FrameSnapshot | None = None
        self._generation = 0
        self._replacements = 0

    def update(
        self,
        frame: XRFrameData,
        received_monotonic: float | None = None,
    ) -> FrameSnapshot:
        received_at = (
            time.monotonic() if received_monotonic is None else received_monotonic
        )
        if not isinstance(received_at, (int, float)) or not math.isfinite(received_at):
            raise ValueError("received_monotonic must be finite")

        with self._lock:
            self._generation += 1
            snapshot = FrameSnapshot(frame, float(received_at), self._generation)
            if self._pending is not None:
                self._replacements += 1
            self._latest = snapshot
            self._pending = snapshot
            return snapshot

    def take_latest(self) -> FrameSnapshot | None:
        """Return the freshest unpublished frame and mark it consumed."""
        with self._lock:
            snapshot = self._pending
            self._pending = None
            return snapshot

    def latest(self) -> FrameSnapshot | None:
        with self._lock:
            return self._latest

    def age_ms(self, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now_monotonic must be finite")
        with self._lock:
            if self._latest is None:
                return math.inf
            return max(0.0, (float(now) - self._latest.received_monotonic) * 1000.0)

    @property
    def replacements(self) -> int:
        with self._lock:
            return self._replacements
