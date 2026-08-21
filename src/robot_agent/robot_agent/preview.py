"""On-demand MJPEG preview served from the Agent's latest camera frames."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit


MAX_JPEG_BYTES = 16 * 1024 * 1024
MAX_HEALTH_SNAPSHOT_BYTES = 256 * 1024
STREAM_PATH = re.compile(r"^/robots/([^/]+)/cameras/([^/]+)\.(mjpg|jpg)$")
HEALTH_SNAPSHOT_PATH = re.compile(r"^/health/v1/robots/([^/]+)/latest$")
ALLOWED_PREVIEW_ORIGINS = frozenset(
    {
        "http://127.0.0.1:18091",
        "http://localhost:18091",
        "http://[::1]:18091",
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "http://[::1]:8090",
    }
)


@dataclass(frozen=True)
class PreviewFrame:
    data: bytes
    sequence: int
    received_monotonic: float


@dataclass(frozen=True)
class HealthSnapshotDocument:
    data: bytes
    boot_id: str
    sequence: int


class PreviewFrameStore:
    """Keep only the latest JPEG for each robot camera."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frames: dict[tuple[str, str], PreviewFrame] = {}

    def update(
        self,
        robot_id: str,
        source_key: str,
        data: bytes,
        *,
        received_monotonic: float | None = None,
    ) -> PreviewFrame:
        jpeg = bytes(data)
        if not jpeg.startswith(b"\xff\xd8"):
            raise ValueError("camera preview payload is not JPEG")
        if len(jpeg) > MAX_JPEG_BYTES:
            raise ValueError("camera preview JPEG exceeds 16 MiB")
        received_at = time.monotonic() if received_monotonic is None else float(received_monotonic)
        identity = (robot_id, source_key)
        with self._condition:
            previous = self._frames.get(identity)
            frame = PreviewFrame(
                data=jpeg,
                sequence=1 if previous is None else previous.sequence + 1,
                received_monotonic=received_at,
            )
            self._frames[identity] = frame
            self._condition.notify_all()
            return frame

    def get(self, robot_id: str, source_key: str) -> PreviewFrame | None:
        with self._condition:
            return self._frames.get((robot_id, source_key))

    def wait_for_newer(
        self,
        robot_id: str,
        source_key: str,
        sequence: int,
        *,
        timeout_s: float,
    ) -> PreviewFrame | None:
        identity = (robot_id, source_key)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                frame = self._frames.get(identity)
                if frame is not None and frame.sequence > sequence:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                self._condition.wait(remaining)


class HealthSnapshotStore:
    """Keep one immutable versioned health document per robot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, HealthSnapshotDocument] = {}

    def update(self, snapshot: dict) -> HealthSnapshotDocument:
        robot_id = snapshot.get("robot_id")
        boot_id = snapshot.get("boot_id")
        sequence = snapshot.get("sequence")
        if not isinstance(robot_id, str) or not robot_id:
            raise ValueError("health snapshot robot_id is invalid")
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("health snapshot boot_id is invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("health snapshot sequence is invalid")
        try:
            data = json.dumps(
                snapshot,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("health snapshot is not JSON serializable") from exc
        if len(data) > MAX_HEALTH_SNAPSHOT_BYTES:
            raise ValueError("health snapshot exceeds 256 KiB")
        document = HealthSnapshotDocument(data, boot_id, sequence)
        with self._lock:
            previous = self._snapshots.get(robot_id)
            if (
                previous is not None
                and previous.boot_id == boot_id
                and sequence <= previous.sequence
            ):
                raise ValueError("health snapshot sequence did not advance")
            self._snapshots[robot_id] = document
        return document

    def get(self, robot_id: str) -> HealthSnapshotDocument | None:
        with self._lock:
            return self._snapshots.get(robot_id)


class _PreviewHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        frame_store: PreviewFrameStore,
        health_store: HealthSnapshotStore,
    ):
        self.frame_store = frame_store
        self.health_store = health_store
        super().__init__(address, _PreviewRequestHandler)


class _PreviewRequestHandler(BaseHTTPRequestHandler):
    server: _PreviewHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send_cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_PREVIEW_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, "application/json", body)
            return

        health_match = HEALTH_SNAPSHOT_PATH.fullmatch(path)
        if health_match is not None:
            document = self.server.health_store.get(unquote(health_match.group(1)))
            if document is None:
                self._send_bytes(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"health snapshot unavailable",
                )
                return
            self._send_bytes(HTTPStatus.OK, "application/json", document.data)
            return

        match = STREAM_PATH.fullmatch(path)
        if match is None:
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found")
            return
        robot_id, source_key, extension = (
            unquote(match.group(1)),
            unquote(match.group(2)),
            match.group(3),
        )
        frame = self.server.frame_store.get(robot_id, source_key)
        if frame is None:
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"frame unavailable")
            return
        if extension == "jpg":
            self._send_bytes(HTTPStatus.OK, "image/jpeg", frame.data)
            return
        self._stream(robot_id, source_key, frame)

    def _stream(self, robot_id: str, source_key: str, frame: PreviewFrame) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        current = frame
        try:
            while True:
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(current.data)}\r\n".encode("ascii")
                    + f"X-Frame-Sequence: {current.sequence}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(header)
                self.wfile.write(current.data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                newer = self.server.frame_store.wait_for_newer(
                    robot_id,
                    source_key,
                    current.sequence,
                    timeout_s=5.0,
                )
                if newer is not None:
                    current = newer
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class CameraPreviewServer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self.frame_store = PreviewFrameStore()
        self.health_store = HealthSnapshotStore()
        self._server = _PreviewHttpServer(
            (host, port),
            self.frame_store,
            self.health_store,
        )
        actual_port = self._server.server_address[1]
        self.public_base_url = (public_base_url or f"http://{host}:{actual_port}").rstrip("/")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="camera-preview",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def stream_url(self, robot_id: str, source_key: str) -> str:
        return (
            f"{self.public_base_url}/robots/{quote(robot_id, safe='')}"
            f"/cameras/{quote(source_key, safe='')}.mjpg"
        )

    def health_snapshot_url(self, robot_id: str) -> str:
        return (
            f"{self.public_base_url}/health/v1/robots/"
            f"{quote(robot_id, safe='')}/latest"
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])
