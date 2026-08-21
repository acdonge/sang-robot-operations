"""Reconnectable TCP server for newline-delimited XR frames."""

from __future__ import annotations

from dataclasses import dataclass
import math
import socket
import threading
import time
from typing import Callable

from xr_teleop_bridge.protocol import ProtocolError, XRFrameData, decode_frame


FrameCallback = Callable[[XRFrameData, float], None]


@dataclass(frozen=True)
class ServerStats:
    listening: bool
    connected: bool
    bound_port: int
    peer: str
    connections_total: int
    accepted_frames: int
    malformed_frames: int
    out_of_order_frames: int
    callback_errors: int
    sequence_gaps: int
    bytes_received: int
    last_sequence: int | None
    last_received_monotonic: float | None
    last_error: str


class TcpFrameServer:
    """Accept one XR producer at a time and survive client reconnects."""

    def __init__(
        self,
        host: str,
        port: int,
        on_frame: FrameCallback,
        *,
        max_line_bytes: int = 65536,
        socket_timeout_s: float = 0.2,
    ) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("host must be non-empty")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if max_line_bytes < 1024:
            raise ValueError("max_line_bytes must be at least 1024")
        if not math.isfinite(socket_timeout_s) or socket_timeout_s <= 0.0:
            raise ValueError("socket_timeout_s must be positive and finite")

        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.max_line_bytes = max_line_bytes
        self.socket_timeout_s = socket_timeout_s

        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._client: socket.socket | None = None
        self._socket_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._startup_error: BaseException | None = None

        self._listening = False
        self._connected = False
        self._bound_port = port
        self._peer = ""
        self._connections_total = 0
        self._accepted_frames = 0
        self._malformed_frames = 0
        self._out_of_order_frames = 0
        self._callback_errors = 0
        self._sequence_gaps = 0
        self._bytes_received = 0
        self._last_sequence: int | None = None
        self._last_received_monotonic: float | None = None
        self._last_error = ""

    @property
    def bound_port(self) -> int:
        with self._stats_lock:
            return self._bound_port

    def start(self, timeout_s: float = 2.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="xr-tcp-frame-server",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout_s):
            self.stop()
            raise RuntimeError("timed out while starting XR TCP server")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(f"could not start XR TCP server: {error}") from error

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop_event.set()
        with self._socket_lock:
            sockets = (self._client, self._listener)
        for item in sockets:
            if item is None:
                continue
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                item.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        self._thread = None

    def stats(self) -> ServerStats:
        with self._stats_lock:
            return ServerStats(
                listening=self._listening,
                connected=self._connected,
                bound_port=self._bound_port,
                peer=self._peer,
                connections_total=self._connections_total,
                accepted_frames=self._accepted_frames,
                malformed_frames=self._malformed_frames,
                out_of_order_frames=self._out_of_order_frames,
                callback_errors=self._callback_errors,
                sequence_gaps=self._sequence_gaps,
                bytes_received=self._bytes_received,
                last_sequence=self._last_sequence,
                last_received_monotonic=self._last_received_monotonic,
                last_error=self._last_error,
            )

    def _run(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.settimeout(self.socket_timeout_s)
        except OSError as exc:
            listener.close()
            self._startup_error = exc
            self._set_error(str(exc))
            self._ready_event.set()
            return

        with self._socket_lock:
            self._listener = listener
        with self._stats_lock:
            self._bound_port = int(listener.getsockname()[1])
            self._listening = True
        self._ready_event.set()

        try:
            while not self._stop_event.is_set():
                try:
                    client, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self._set_error(str(exc))
                    break
                self._serve_client(client, address)
        finally:
            with self._socket_lock:
                if self._listener is listener:
                    self._listener = None
            try:
                listener.close()
            except OSError:
                pass
            with self._stats_lock:
                self._listening = False
                self._connected = False

    def _serve_client(self, client: socket.socket, address: tuple[str, int]) -> None:
        client.settimeout(self.socket_timeout_s)
        with self._socket_lock:
            self._client = client
        with self._stats_lock:
            self._connected = True
            self._peer = f"{address[0]}:{address[1]}"
            self._connections_total += 1

        buffer = bytearray()
        last_sequence: int | None = None
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = client.recv(8192)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self._set_error(str(exc))
                    break
                if not chunk:
                    break

                with self._stats_lock:
                    self._bytes_received += len(chunk)
                buffer.extend(chunk)

                while True:
                    newline_index = buffer.find(b"\n")
                    if newline_index < 0:
                        break
                    line = bytes(buffer[:newline_index]).strip()
                    del buffer[: newline_index + 1]
                    if len(line) > self.max_line_bytes:
                        self._record_malformed("frame exceeds max_line_bytes")
                        continue
                    if not line:
                        continue
                    last_sequence = self._handle_line(line, last_sequence)

                if len(buffer) > self.max_line_bytes:
                    self._record_malformed("unterminated frame exceeds max_line_bytes")
                    break
        finally:
            if buffer.strip() and not self._stop_event.is_set():
                self._record_malformed("connection closed with an incomplete frame")
            with self._socket_lock:
                if self._client is client:
                    self._client = None
            try:
                client.close()
            except OSError:
                pass
            with self._stats_lock:
                self._connected = False

    def _handle_line(self, line: bytes, last_sequence: int | None) -> int | None:
        try:
            frame = decode_frame(line)
        except ProtocolError as exc:
            self._record_malformed(str(exc))
            return last_sequence

        if last_sequence is not None and frame.sequence <= last_sequence:
            with self._stats_lock:
                self._out_of_order_frames += 1
                self._last_error = (
                    f"out-of-order sequence {frame.sequence} after {last_sequence}"
                )
            return last_sequence

        if last_sequence is not None and frame.sequence > last_sequence + 1:
            with self._stats_lock:
                self._sequence_gaps += frame.sequence - last_sequence - 1

        received_monotonic = time.monotonic()
        try:
            self.on_frame(frame, received_monotonic)
        except Exception as exc:  # Keep the transport alive if the consumer fails.
            with self._stats_lock:
                self._callback_errors += 1
                self._last_error = f"frame callback failed: {exc}"
            return last_sequence

        with self._stats_lock:
            self._accepted_frames += 1
            self._last_sequence = frame.sequence
            self._last_received_monotonic = received_monotonic
            self._last_error = ""
        return frame.sequence

    def _record_malformed(self, error: str) -> None:
        with self._stats_lock:
            self._malformed_frames += 1
            self._last_error = error

    def _set_error(self, error: str) -> None:
        with self._stats_lock:
            self._last_error = error
