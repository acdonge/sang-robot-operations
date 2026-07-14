"""FastAPI gateway for in-process manifest-driven recorder nodes."""

import argparse
import asyncio
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


STARTABLE_STATES = frozenset({"IDLE", "READY"})


class StartRequest(BaseModel):
    task: str


class GatewayAPIError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


@dataclass
class RobotHandle:
    node: Any
    episode_started_monotonic: float | None = None


def state_conflict(action: str, state: str) -> str | None:
    """Return the contract error for a disallowed state transition."""
    if action == "start" and state not in STARTABLE_STATES:
        return f"record_start requires IDLE or READY, got {state}"
    if action == "stop" and state != "RECORDING":
        return f"record_stop requires RECORDING, got {state}"
    return None


def contract_sources(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project detailed node health onto the UI's stable source schema."""
    sources = {}
    for key, source in health["sources"].items():
        age_ms = source["age_ms"]
        if not isinstance(age_ms, (int, float)) or not math.isfinite(age_ms):
            age_ms = None
        sources[key] = {
            "age_ms": age_ms,
            "seq": source["seq"],
            "fresh": source["fresh"],
        }
    return sources


def robot_descriptor(node: Any) -> dict[str, Any]:
    manifest = node.manifest
    return {
        "robot_id": manifest.robot.robot_id,
        "model": manifest.robot.model,
        "cameras": [camera.key for camera in manifest.interfaces.cameras],
        "fps": manifest.recording.fps,
    }


def status_payload(handle: RobotHandle) -> dict[str, Any]:
    node_status = handle.node.get_status()
    fsm = node_status["fsm"]
    started = handle.episode_started_monotonic
    elapsed_s = 0.0 if started is None else max(0.0, time.monotonic() - started)
    return {
        "type": "status",
        "robot_id": handle.node.manifest.robot.robot_id,
        "fsm_state": fsm["state"],
        "sources": contract_sources(node_status),
        "episode": {"frames": fsm["frame_count"], "elapsed_s": elapsed_s},
        "message": fsm["failure_reason"] or "",
    }


def mjpeg_part(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
        + jpeg
        + b"\r\n"
    )


async def wait_for_commit(node: Any, timeout_s: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        fsm = node.get_status()["fsm"]
        if fsm["state"] == "READY":
            return fsm
        if fsm["state"] == "FAULT":
            raise GatewayAPIError(500, fsm["failure_reason"] or "commit failed")
        if fsm["state"] != "COMMITTING":
            raise GatewayAPIError(
                500, f"commit left COMMITTING unexpectedly: {fsm['state']}"
            )
        if time.monotonic() >= deadline:
            raise GatewayAPIError(504, "commit did not complete within 60 seconds")
        await asyncio.sleep(0.05)


def create_app(nodes: Iterable[Any], *, ui_dir: str | Path = "/ws/ui") -> FastAPI:
    handles: dict[str, RobotHandle] = {}
    for node in nodes:
        robot_id = node.manifest.robot.robot_id
        if robot_id in handles:
            raise ValueError(f"duplicate robot_id: {robot_id}")
        handles[robot_id] = RobotHandle(node)

    app = FastAPI()

    @app.exception_handler(GatewayAPIError)
    async def gateway_error_handler(
        request: Request, exc: GatewayAPIError
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    def get_handle(robot_id: str) -> RobotHandle:
        try:
            return handles[robot_id]
        except KeyError as exc:
            raise GatewayAPIError(404, f"unknown robot_id: {robot_id}") from exc

    @app.get("/api/robots")
    async def list_robots() -> list[dict[str, Any]]:
        return [robot_descriptor(handle.node) for handle in handles.values()]

    @app.get("/api/robots/{robot_id}/health")
    async def robot_health(robot_id: str) -> dict[str, Any]:
        node = get_handle(robot_id).node
        state = node.get_status()["fsm"]["state"]
        health = node.get_health()
        return {"fsm_state": state, "sources": contract_sources(health)}

    @app.post("/api/robots/{robot_id}/record/start")
    async def record_start(robot_id: str, body: StartRequest):
        handle = get_handle(robot_id)
        state = handle.node.get_status()["fsm"]["state"]
        conflict = state_conflict("start", state)
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            started = handle.node.start_recording(body.task)
        except ValueError as exc:
            raise GatewayAPIError(400, str(exc)) from exc
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        fsm = handle.node.get_status()["fsm"]
        if not started:
            raise GatewayAPIError(500, fsm["failure_reason"] or "preflight failed")
        handle.episode_started_monotonic = time.monotonic()
        return {"session_id": fsm["session_id"]}

    @app.post("/api/robots/{robot_id}/record/stop")
    async def record_stop(robot_id: str):
        handle = get_handle(robot_id)
        before = handle.node.get_status()["fsm"]
        conflict = state_conflict("stop", before["state"])
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            started = handle.node.stop_recording()
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        if not started:
            raise GatewayAPIError(500, "commit failed to start")
        completed = await wait_for_commit(handle.node)
        return {
            "session_id": completed["session_id"],
            "dataset_path": completed["raw_path"],
        }

    @app.post("/api/robots/{robot_id}/record/abort")
    async def record_abort(robot_id: str) -> dict[str, bool]:
        handle = get_handle(robot_id)
        handle.node.abort_recording()
        return {"aborted": True}

    @app.websocket("/ws")
    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        disconnect = asyncio.create_task(websocket.receive())
        try:
            while True:
                for handle in handles.values():
                    await websocket.send_json(status_payload(handle))
                done, _ = await asyncio.wait({disconnect}, timeout=0.2)
                if done:
                    break
        except (RuntimeError, WebSocketDisconnect):
            pass
        finally:
            disconnect.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect

    @app.get("/api/robots/{robot_id}/cameras/{camera_key}/stream")
    async def camera_stream(robot_id: str, camera_key: str) -> StreamingResponse:
        node = get_handle(robot_id).node
        camera_keys = {
            camera.key for camera in node.manifest.interfaces.cameras
        }
        if camera_key not in camera_keys:
            raise GatewayAPIError(404, f"unknown camera key: {camera_key}")

        async def frames():
            while True:
                jpeg = node.get_camera_jpeg(camera_key)
                if jpeg is not None:
                    yield mjpeg_part(jpeg)
                await asyncio.sleep(0.1)

        return StreamingResponse(
            frames(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    return app


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V2 recorder FastAPI gateway")
    parser.add_argument(
        "manifest_path",
        nargs="+",
        type=Path,
        help="one or more recorder manifest paths",
    )
    parser.add_argument("--ui-dir", type=Path, default=Path("/ws/ui"))
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)

    import rclpy
    from rclpy.executors import MultiThreadedExecutor
    import uvicorn

    from recorder_core.recorder_node import RecorderNode

    rclpy.init(args=[])
    executor = MultiThreadedExecutor()
    nodes = []
    executor_thread = None
    try:
        for index, manifest_path in enumerate(options.manifest_path):
            node = RecorderNode(
                manifest_path,
                node_name=f"recorder_core_gateway_{index}",
            )
            nodes.append(node)
            executor.add_node(node)

        app = create_app(nodes, ui_dir=options.ui_dir)
        executor_thread = threading.Thread(
            target=executor.spin,
            name="rclpy-executor",
            daemon=True,
        )
        executor_thread.start()
        uvicorn.run(app, host="0.0.0.0", port=8080)
    finally:
        executor.shutdown()
        if executor_thread is not None:
            executor_thread.join(timeout=5.0)
        for node in nodes:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
