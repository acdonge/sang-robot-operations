"""Synthetic Galaxy XR stream used before hardware is connected."""

from __future__ import annotations

import argparse
import math
import socket
import time

from xr_teleop_bridge.protocol import (
    ControllerData,
    PoseData,
    XRFrameData,
    encode_frame,
)


BUTTON_PRIMARY = 1


def sample_frame(sequence: int, elapsed_s: float, source: str) -> XRFrameData:
    sway = 0.08 * math.sin(elapsed_s * 1.3)
    yaw = 0.2 * math.sin(elapsed_s * 0.7)
    orientation = (0.0, math.sin(yaw / 2.0), 0.0, math.cos(yaw / 2.0))
    head = PoseData(
        position=(0.0, 1.65, 0.0),
        orientation=orientation,
        valid=True,
        tracked=True,
    )
    left_grip = PoseData(
        position=(-0.25 + sway, 1.25, -0.35),
        orientation=orientation,
        valid=True,
        tracked=True,
    )
    right_grip = PoseData(
        position=(0.25 - sway, 1.25, -0.35),
        orientation=orientation,
        valid=True,
        tracked=True,
    )
    trigger = 0.5 + 0.5 * math.sin(elapsed_s * 2.0)
    primary_pressed = int(elapsed_s) % 2 == 1
    zero_velocity = (0.0, 0.0, 0.0)
    left = ControllerData(
        connected=True,
        grip=left_grip,
        aim=left_grip,
        linear_velocity=zero_velocity,
        angular_velocity=zero_velocity,
        trigger=trigger,
        squeeze=0.0,
        thumbstick=(0.0, 0.0),
        buttons=0,
    )
    right = ControllerData(
        connected=True,
        grip=right_grip,
        aim=right_grip,
        linear_velocity=zero_velocity,
        angular_velocity=zero_velocity,
        trigger=trigger,
        squeeze=0.75 if primary_pressed else 0.0,
        thumbstick=(sway, 0.0),
        buttons=BUTTON_PRIMARY if primary_pressed else 0,
    )
    return XRFrameData(
        sequence=sequence,
        device_time_ns=time.monotonic_ns(),
        source=source,
        frame_id="xr_local",
        head=head,
        left=left,
        right=right,
    )


def connect_with_retry(host: str, port: int, timeout_s: float) -> socket.socket:
    deadline = time.monotonic() + timeout_s
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return socket.create_connection((host, port), timeout=1.0)
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise ConnectionError(f"could not connect to {host}:{port}: {last_error}")


def send_frames(
    host: str,
    port: int,
    *,
    hz: float,
    frames: int,
    source: str,
) -> None:
    if not math.isfinite(hz) or hz <= 0.0:
        raise ValueError("hz must be positive and finite")
    if frames <= 0:
        raise ValueError("frames must be positive")

    period_s = 1.0 / hz
    started = time.monotonic()
    with connect_with_retry(host, port, timeout_s=5.0) as connection:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for index in range(frames):
            target_time = started + index * period_s
            remaining = target_time - time.monotonic()
            if remaining > 0.0:
                time.sleep(remaining)
            elapsed_s = time.monotonic() - started
            connection.sendall(
                encode_frame(sample_frame(index + 1, elapsed_s, source))
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--hz", type=float, default=60.0)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--source", default="mock_galaxy_xr")
    args = parser.parse_args()

    send_frames(
        args.host,
        args.port,
        hz=args.hz,
        frames=args.frames,
        source=args.source,
    )
    print(
        f"sent {args.frames} xr_frame_v1 frames to "
        f"{args.host}:{args.port} at {args.hz:.1f} Hz"
    )


if __name__ == "__main__":
    main()
