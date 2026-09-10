"""Loopback-only lifecycle and preview service for one fixed Unity Player."""

from __future__ import annotations

import argparse
import atexit
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PREVIEW_BOUNDARY = "sang-unity-frame"
MAX_PREVIEW_BYTES = 4 * 1024 * 1024


def _isoformat_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UnityRuntimeSupervisor:
    """Owns one allowlisted, output-disconnected Unity simulation process."""

    def __init__(
        self,
        *,
        player_path: str | Path,
        bridge_root: str | Path,
        marker_path: str | Path,
        log_path: str | Path,
    ) -> None:
        self.player_path = Path(player_path).expanduser().resolve()
        self.bridge_root = Path(bridge_root).expanduser().resolve()
        self.marker_path = Path(marker_path).expanduser().resolve()
        self.log_path = Path(log_path).expanduser().resolve()
        self.preview_frame_path = self.bridge_root / "preview" / "frame.jpg"
        self.preview_metadata_path = self.bridge_root / "preview" / "frame.json"
        self.heartbeat_path = self.bridge_root / "heartbeat.json"
        self._lock = threading.RLock()
        self._preview_slots = threading.BoundedSemaphore(value=2)
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at: str | None = None
        self._last_exit_code: int | None = None
        self._validate_build()
        atexit.register(self.stop)

    def _validate_build(self) -> dict[str, Any]:
        if not self.player_path.is_file() or not os.access(self.player_path, os.X_OK):
            raise ValueError(f"Unity Player is not executable: {self.player_path}")
        manifest_path = Path(f"{self.player_path}.simulation.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unity simulation manifest is unavailable: {exc}") from exc
        required = {
            "schema_version": 1,
            "build_role": "simulation_executor",
            "physical_output_connected": False,
            "compile_define": "SANG_SIMULATION_PLAYER",
            "control_surface": "external_workflow_ui",
        }
        mismatched = [
            key for key, expected in required.items()
            if manifest.get(key) != expected
        ]
        if mismatched:
            raise ValueError(
                "Unity Player is not an output-disconnected simulation build: "
                + ", ".join(mismatched)
            )
        return manifest

    def _refresh_process(self) -> None:
        if self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        self._last_exit_code = exit_code
        self._process = None

    @staticmethod
    def _json_status(path: Path, *, max_age_s: float) -> dict[str, Any] | None:
        try:
            age_s = max(0.0, time.time() - path.stat().st_mtime)
            if age_s > max_age_s:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            return {**value, "age_s": age_s}
        except (OSError, json.JSONDecodeError):
            return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            running = self._process is not None
            heartbeat = self._json_status(self.heartbeat_path, max_age_s=3.0)
            preview = self._json_status(self.preview_metadata_path, max_age_s=3.0)
            return {
                "schema_version": 1,
                "configured": True,
                "running": running,
                "pid": None if self._process is None else self._process.pid,
                "started_at": self._started_at,
                "last_exit_code": self._last_exit_code,
                "scenario_id": self._validate_build().get("scenario_id"),
                "physical_output_connected": False,
                "heartbeat": heartbeat if running else None,
                "ready": bool(running and heartbeat and heartbeat.get("ready")),
                "preview": preview if running else None,
                "preview_available": bool(
                    running and preview and self.preview_frame_path.is_file()
                ),
            }

    def _write_marker(self) -> None:
        self.bridge_root.mkdir(parents=True, exist_ok=True)
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.marker_path.with_name(
            f"{self.marker_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps({"bridge_root": str(self.bridge_root)}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.marker_path)

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            if self._process is not None:
                return {**self.status(), "idempotent_replay": True}
            self._validate_build()
            self._write_marker()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (
                self.heartbeat_path,
                self.preview_frame_path,
                self.preview_metadata_path,
            ):
                stale.unlink(missing_ok=True)

            environment = dict(os.environ)
            environment.pop("IITP_RB5_SIM_TO_REAL", None)
            environment.pop("IITP_OMY_SIM_TO_REAL", None)
            environment["SANG_UNITY_ORCHESTRATOR"] = "1"
            command = [
                str(self.player_path),
                "-screen-fullscreen",
                "0",
                "-screen-width",
                "1280",
                "-screen-height",
                "720",
                "-logFile",
                str(self.log_path),
            ]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=self.player_path.parent,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            except OSError as exc:
                raise RuntimeError(f"Unity Player could not start: {exc}") from exc
            self._started_at = _isoformat_now()
            self._last_exit_code = None
            return {**self.status(), "idempotent_replay": False}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            process = self._process
            if process is None:
                return {**self.status(), "idempotent_replay": True}
            process.terminate()
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
            self._last_exit_code = process.returncode
            self._process = None
            return {**self.status(), "idempotent_replay": False}

    def preview_response(self) -> StreamingResponse:
        if not self._preview_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Unity preview client limit reached")
        released = False
        release_lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            self._preview_slots.release()

        def frames():
            last_mtime_ns = -1
            try:
                while True:
                    with self._lock:
                        self._refresh_process()
                        running = self._process is not None
                    try:
                        stat = self.preview_frame_path.stat()
                        if stat.st_mtime_ns != last_mtime_ns:
                            frame = self.preview_frame_path.read_bytes()
                            if (
                                4 <= len(frame) <= MAX_PREVIEW_BYTES
                                and frame.startswith(b"\xff\xd8")
                                and frame.endswith(b"\xff\xd9")
                            ):
                                last_mtime_ns = stat.st_mtime_ns
                                yield (
                                    f"--{PREVIEW_BOUNDARY}\r\n"
                                    "Content-Type: image/jpeg\r\n"
                                    f"Content-Length: {len(frame)}\r\n\r\n"
                                ).encode("ascii") + frame + b"\r\n"
                    except OSError:
                        pass
                    if not running:
                        return
                    time.sleep(0.05)
            finally:
                release()

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={PREVIEW_BOUNDARY}",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(release),
        )


def create_runtime_app(supervisor: UnityRuntimeSupervisor) -> FastAPI:
    app = FastAPI(title="SANG Unity Runtime Supervisor", version="1")

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return supervisor.status()

    @app.post("/start")
    def start() -> dict[str, Any]:
        try:
            return supervisor.start()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/stop")
    def stop() -> dict[str, Any]:
        return supervisor.stop()

    @app.get("/preview.mjpg")
    def preview() -> StreamingResponse:
        return supervisor.preview_response()

    return app


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Loopback-only supervisor for one SANG Unity simulation Player"
    )
    parser.add_argument("--player-path", type=Path, required=True)
    parser.add_argument("--bridge-root", type=Path, required=True)
    parser.add_argument(
        "--marker-path",
        type=Path,
        default=Path("/tmp/sang_unity_orchestrator_enabled"),
    )
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18093)
    options = parser.parse_args(args)
    if options.host not in LOOPBACK_HOSTS:
        parser.error("Unity runtime supervisor must bind to loopback")
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    import uvicorn

    supervisor = UnityRuntimeSupervisor(
        player_path=options.player_path,
        bridge_root=options.bridge_root,
        marker_path=options.marker_path,
        log_path=options.log_path,
    )
    uvicorn.run(
        create_runtime_app(supervisor),
        host=options.host,
        port=options.port,
    )


if __name__ == "__main__":
    main()
