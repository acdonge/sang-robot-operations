#!/usr/bin/env python3
"""Stream one X11 monitor to the Galaxy XR app over the XRDS protocol."""

from __future__ import annotations

import argparse
import io
import os
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass

from PIL import Image, ImageGrab


MAGIC = b"XRDS"
PROTOCOL_VERSION = 1
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
HEADER = struct.Struct("!4sIIIII")


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool


def connected_monitors(display: str) -> list[Monitor]:
    output = subprocess.run(
        ["xrandr", "--display", display, "--query"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pattern = re.compile(
        r"^(?P<name>\S+) connected(?P<primary> primary)? "
        r"(?P<width>\d+)x(?P<height>\d+)"
        r"(?P<x>[+-]\d+)(?P<y>[+-]\d+)"
    )
    monitors: list[Monitor] = []
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        monitors.append(
            Monitor(
                name=match.group("name"),
                width=int(match.group("width")),
                height=int(match.group("height")),
                x=int(match.group("x")),
                y=int(match.group("y")),
                primary=bool(match.group("primary")),
            )
        )
    if not monitors:
        raise RuntimeError(f"no active X11 monitors found on {display}")
    return monitors


def choose_monitor(monitors: list[Monitor], requested: str) -> Monitor:
    if requested == "primary":
        return next((monitor for monitor in monitors if monitor.primary), monitors[0])
    for monitor in monitors:
        if monitor.name == requested:
            return monitor
    names = ", ".join(monitor.name for monitor in monitors)
    raise RuntimeError(f"monitor {requested!r} not found; available: {names}")


def capture_jpeg(display: str, monitor: Monitor, quality: int) -> bytes:
    image = ImageGrab.grab(
        bbox=(
            monitor.x,
            monitor.y,
            monitor.x + monitor.width,
            monitor.y + monitor.height,
        ),
        xdisplay=display,
    )
    if image.mode != "RGB":
        image = image.convert("RGB")
    if image.size != (FRAME_WIDTH, FRAME_HEIGHT):
        image = image.resize(
            (FRAME_WIDTH, FRAME_HEIGHT),
            Image.Resampling.BILINEAR,
        )
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=quality, subsampling=2)
    return encoded.getvalue()


def serve(args: argparse.Namespace) -> None:
    monitors = connected_monitors(args.display)
    monitor = choose_monitor(monitors, args.monitor)
    frame_period = 1.0 / args.fps

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        print(
            f"Galaxy XR desktop stream listening on tcp://{args.host}:{args.port}",
            flush=True,
        )
        print(
            f"Capturing {monitor.name} {monitor.width}x{monitor.height}"
            f"{monitor.x:+d}{monitor.y:+d} -> {FRAME_WIDTH}x{FRAME_HEIGHT} "
            f"at {args.fps:.1f} fps",
            flush=True,
        )

        sequence = 0
        while True:
            connection, address = server.accept()
            print(f"Headset connected from {address[0]}:{address[1]}", flush=True)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                with connection:
                    next_frame_at = time.monotonic()
                    while True:
                        payload = capture_jpeg(args.display, monitor, args.quality)
                        sequence = (sequence + 1) & 0xFFFFFFFF
                        header = HEADER.pack(
                            MAGIC,
                            PROTOCOL_VERSION,
                            FRAME_WIDTH,
                            FRAME_HEIGHT,
                            len(payload),
                            sequence,
                        )
                        connection.sendall(header)
                        connection.sendall(payload)
                        next_frame_at += frame_period
                        delay = next_frame_at - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        else:
                            next_frame_at = time.monotonic()
            except (BrokenPipeError, ConnectionError, OSError) as error:
                print(f"Headset disconnected: {error}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream an X11 monitor to the Galaxy XR spatial desktop window."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15557)
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    parser.add_argument(
        "--monitor",
        default="primary",
        help="xrandr output name, or 'primary' (default)",
    )
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--quality", type=int, default=78)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if 5555 <= args.port <= 5585 and args.port % 2 == 1:
        parser.error("--port conflicts with ADB emulator discovery")
    if not 1.0 <= args.fps <= 30.0:
        parser.error("--fps must be between 1 and 30")
    if not 30 <= args.quality <= 95:
        parser.error("--quality must be between 30 and 95")
    return args


if __name__ == "__main__":
    try:
        serve(parse_args())
    except KeyboardInterrupt:
        print("\nDesktop stream stopped.", flush=True)
