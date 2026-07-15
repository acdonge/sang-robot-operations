"""FastAPI gateway for in-process manifest-driven recorder nodes."""

import argparse
import asyncio
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from recorder_core.hf_upload import HfUploadManager, UploadPreflightError
from recorder_core.inference_manager import (
    SAFETY_WARNING,
    InferenceConflictError,
    InferenceManager,
    InferencePreflightError,
)
from recorder_core.store_index import scan_store, session_path_for_dataset
from recorder_core.train_manager import (
    TrainConflictError,
    TrainManager,
    TrainPreflightError,
)


STARTABLE_STATES = frozenset({"IDLE", "READY"})


class StartRequest(BaseModel):
    task: str
    task_instructions: list[str] = Field(default_factory=list)
    episode_time_s: float = 0.0
    reset_time_s: float = 0.0
    num_episodes: int = 0
    rosbag: bool = False


class UploadRequest(BaseModel):
    repo_id: str = Field(min_length=1)
    token: SecretStr
    private: bool = False


class TrainRequest(BaseModel):
    dataset_id: str = Field(min_length=1)
    policy_type: Literal["act"]
    steps: int = Field(ge=1)


class InferenceRequest(BaseModel):
    run_id: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)


class GatewayAPIError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


@dataclass
class RobotHandle:
    node: Any
    episode_started_monotonic: float | None = None
    system_metrics: "SystemMetrics | None" = None


class SystemMetrics:
    """Sample CPU, available RAM, and store filesystem without requiring psutil."""

    def __init__(self, store_path: str | Path) -> None:
        self.store_path = Path(store_path)
        self._previous_cpu: tuple[int, int] | None = None
        try:
            import psutil
        except ImportError:
            self._psutil = None
        else:
            self._psutil = psutil

    @staticmethod
    def _proc_cpu_times() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def _cpu_pct(self) -> float:
        if self._psutil is not None:
            return float(self._psutil.cpu_percent(interval=None))
        current = self._proc_cpu_times()
        previous = self._previous_cpu
        self._previous_cpu = current
        if previous is None:
            return 0.0
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        return 100.0 * (1.0 - idle_delta / total_delta)

    def _ram_free_gb(self) -> float:
        if self._psutil is not None:
            return float(self._psutil.virtual_memory().available) / (1024**3)
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024**2)
        raise RuntimeError("/proc/meminfo does not contain MemAvailable")

    def _disk_free_gb(self) -> float:
        path = self.store_path
        while not path.exists() and path != path.parent:
            path = path.parent
        return shutil.disk_usage(path).free / (1024**3)

    def sample(self) -> dict[str, float | None]:
        values: dict[str, float | None] = {}
        for key, sampler in (
            ("cpu_pct", self._cpu_pct),
            ("ram_free_gb", self._ram_free_gb),
            ("disk_free_gb", self._disk_free_gb),
        ):
            try:
                values[key] = round(float(sampler()), 3)
            except (OSError, RuntimeError, ValueError):
                values[key] = None
        return values


def state_conflict(action: str, state: str) -> str | None:
    """Return the contract error for a disallowed state transition."""
    if action == "start" and state not in STARTABLE_STATES:
        return f"record_start requires IDLE or READY, got {state}"
    required_state = {
        "stop": "RECORDING",
        "save_next": "RECORDING",
        "retry": "RECORDING",
        "finish": "RECORDING",
        "continue": "RESETTING",
    }.get(action)
    if required_state is not None and state != required_state:
        return f"record_{action} requires {required_state}, got {state}"
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
    elapsed_s = fsm.get("episode_elapsed_s")
    if elapsed_s is None:
        elapsed_s = 0.0 if started is None else max(0.0, time.monotonic() - started)
    metrics = handle.system_metrics
    return {
        "type": "status",
        "robot_id": handle.node.manifest.robot.robot_id,
        "fsm_state": fsm["state"],
        "sources": contract_sources(node_status),
        "episode": {"frames": fsm["frame_count"], "elapsed_s": elapsed_s},
        "episode_index": fsm.get("episode_index", 0),
        "episodes_total": fsm.get("episodes_total", 0),
        "current_instruction": fsm.get("current_instruction"),
        "phase": fsm.get("phase", fsm["state"]),
        "reset_remaining_s": fsm.get("reset_remaining_s"),
        "system": (
            {"cpu_pct": None, "ram_free_gb": None, "disk_free_gb": None}
            if metrics is None
            else metrics.sample()
        ),
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


def create_app(
    nodes: Iterable[Any],
    *,
    ui_dir: str | Path = "/ws/ui",
    store_root: str | Path | None = None,
    upload_manager: HfUploadManager | None = None,
    train_manager: TrainManager | None = None,
    inference_manager: InferenceManager | None = None,
) -> FastAPI:
    handles: dict[str, RobotHandle] = {}
    for node in nodes:
        robot_id = node.manifest.robot.robot_id
        if robot_id in handles:
            raise ValueError(f"duplicate robot_id: {robot_id}")
        handles[robot_id] = RobotHandle(
            node,
            system_metrics=SystemMetrics(node.manifest.recording.dataset_root),
        )

    if store_root is None:
        store_roots = sorted(
            {
                Path(handle.node.manifest.recording.dataset_root).parent.parent
                for handle in handles.values()
            },
            key=str,
        )
    else:
        store_roots = [Path(store_root)]
    uploader = upload_manager or HfUploadManager()
    trainer = train_manager or TrainManager(store_roots[0])
    inferencer = inference_manager or InferenceManager(
        store_roots[0],
        run_supplier=getattr(trainer, "list_runs", lambda: []),
    )

    app = FastAPI()
    app.state.inference_manager = inferencer

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

    def indexed_datasets() -> list[dict[str, Any]]:
        return [
            dataset for root in store_roots for dataset in scan_store(root)
        ]

    def get_dataset(dataset_id: str) -> tuple[dict[str, Any], Path]:
        for root in store_roots:
            for dataset in scan_store(root):
                if dataset["dataset_id"] == dataset_id:
                    return dataset, root
        raise GatewayAPIError(404, f"unknown dataset_id: {dataset_id}")

    @app.get("/api/robots")
    async def list_robots() -> list[dict[str, Any]]:
        return [robot_descriptor(handle.node) for handle in handles.values()]

    @app.get("/api/datasets")
    async def list_datasets() -> list[dict[str, Any]]:
        return indexed_datasets()

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return trainer.list_runs()

    @app.get("/api/policies")
    async def list_policies() -> list[dict[str, Any]]:
        return inferencer.list_policies()

    @app.post("/api/train")
    async def train_start(body: TrainRequest) -> dict[str, str]:
        dataset, _ = get_dataset(body.dataset_id)
        try:
            run_id = trainer.start_train(
                dataset_id=body.dataset_id,
                dataset_path=dataset["path"],
                policy_type=body.policy_type,
                steps=body.steps,
            )
        except (TrainConflictError, TrainPreflightError) as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/stop")
    async def train_stop(run_id: str) -> dict[str, str]:
        try:
            accepted = trainer.stop(run_id)
        except KeyError as exc:
            raise GatewayAPIError(404, f"unknown training run: {run_id}") from exc
        if not accepted:
            raise GatewayAPIError(409, f"training run is not active: {run_id}")
        return {"run_id": run_id}

    @app.post("/api/inference/start")
    def inference_start(body: InferenceRequest) -> dict[str, Any]:
        handle = get_handle(body.robot_id)
        recorder_state = handle.node.get_status()["fsm"]["state"]
        if recorder_state not in STARTABLE_STATES:
            raise GatewayAPIError(
                409,
                "inference requires recorder IDLE or READY, "
                f"got {recorder_state}",
            )
        try:
            status = inferencer.start(body.run_id, handle.node)
        except (InferenceConflictError, InferencePreflightError) as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        return {
            "run_id": body.run_id,
            "robot_id": body.robot_id,
            "state": status["state"],
            "warning": SAFETY_WARNING,
        }

    @app.post("/api/inference/stop")
    def inference_stop() -> dict[str, Any]:
        status = inferencer.stop()
        return {
            "run_id": status["run_id"],
            "robot_id": status["robot_id"],
            "state": status["state"],
        }

    @app.post("/api/datasets/{dataset_id}/upload")
    async def upload_dataset(
        dataset_id: str, body: UploadRequest
    ) -> dict[str, str]:
        dataset, root = get_dataset(dataset_id)
        token = body.token.get_secret_value()
        if not token:
            raise GatewayAPIError(400, "token must not be empty")
        try:
            job_id = uploader.start_upload(
                dataset_id=dataset_id,
                dataset_path=dataset["path"],
                repo_id=body.repo_id,
                token=token,
                private=body.private,
                session_path=session_path_for_dataset(root, dataset_id),
            )
        except UploadPreflightError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        finally:
            token = ""
        return {"job_id": job_id}

    @app.get("/api/upload/{job_id}")
    async def upload_status(job_id: str) -> dict[str, str]:
        try:
            return uploader.get_status(job_id)
        except KeyError as exc:
            raise GatewayAPIError(404, f"unknown upload job: {job_id}") from exc

    @app.get("/api/robots/{robot_id}/health")
    async def robot_health(robot_id: str) -> dict[str, Any]:
        return status_payload(get_handle(robot_id))

    @app.post("/api/robots/{robot_id}/record/start")
    async def record_start(robot_id: str, body: StartRequest):
        handle = get_handle(robot_id)
        state = handle.node.get_status()["fsm"]["state"]
        conflict = state_conflict("start", state)
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            started = handle.node.start_recording(
                body.task,
                task_instructions=body.task_instructions,
                episode_time_s=body.episode_time_s,
                reset_time_s=body.reset_time_s,
                num_episodes=body.num_episodes,
                rosbag=body.rosbag,
            )
        except ValueError as exc:
            raise GatewayAPIError(400, str(exc)) from exc
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        fsm = handle.node.get_status()["fsm"]
        if not started:
            raise GatewayAPIError(500, fsm["failure_reason"] or "preflight failed")
        handle.episode_started_monotonic = time.monotonic()
        return {"session_id": fsm["session_id"]}

    async def finish_recording(handle: RobotHandle) -> dict[str, Any]:
        before = handle.node.get_status()["fsm"]
        conflict = state_conflict("finish", before["state"])
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            started = handle.node.finish_recording()
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        if not started:
            raise GatewayAPIError(500, "commit failed to start")
        completed = await wait_for_commit(handle.node)
        return {
            "session_id": completed["session_id"],
            "dataset_path": completed["raw_path"],
        }

    @app.post("/api/robots/{robot_id}/record/save_next")
    async def record_save_next(robot_id: str) -> dict[str, bool]:
        handle = get_handle(robot_id)
        state = handle.node.get_status()["fsm"]["state"]
        conflict = state_conflict("save_next", state)
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            accepted = handle.node.save_next_recording()
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        return {"accepted": bool(accepted)}

    @app.post("/api/robots/{robot_id}/record/retry")
    async def record_retry(robot_id: str) -> dict[str, bool]:
        handle = get_handle(robot_id)
        state = handle.node.get_status()["fsm"]["state"]
        conflict = state_conflict("retry", state)
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            accepted = handle.node.retry_recording()
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        return {"accepted": bool(accepted)}

    @app.post("/api/robots/{robot_id}/record/continue")
    async def record_continue(robot_id: str) -> dict[str, bool]:
        handle = get_handle(robot_id)
        state = handle.node.get_status()["fsm"]["state"]
        conflict = state_conflict("continue", state)
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        try:
            accepted = handle.node.continue_recording()
        except RuntimeError as exc:
            raise GatewayAPIError(409, str(exc)) from exc
        return {"accepted": bool(accepted)}

    @app.post("/api/robots/{robot_id}/record/finish")
    async def record_finish(robot_id: str):
        return await finish_recording(get_handle(robot_id))

    @app.post("/api/robots/{robot_id}/record/stop")
    async def record_stop(robot_id: str):
        handle = get_handle(robot_id)
        before = handle.node.get_status()["fsm"]
        conflict = state_conflict("stop", before["state"])
        if conflict is not None:
            raise GatewayAPIError(409, conflict)
        return await finish_recording(handle)

    @app.post("/api/robots/{robot_id}/record/abort")
    async def record_abort(robot_id: str) -> dict[str, bool]:
        handle = get_handle(robot_id)
        handle.node.abort_recording()
        return {"aborted": True}

    @app.websocket("/ws")
    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        disconnect = asyncio.create_task(websocket.receive())
        upload_cursor = 0
        train_cursor = 0
        try:
            while True:
                for handle in handles.values():
                    await websocket.send_json(status_payload(handle))
                upload_cursor, upload_events = uploader.events_after(upload_cursor)
                for event in upload_events:
                    await websocket.send_json(event)
                train_cursor, train_events = trainer.events_after(train_cursor)
                for event in train_events:
                    await websocket.send_json(event)
                await websocket.send_json(inferencer.status())
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
    app = None
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
        if app is not None:
            app.state.inference_manager.stop()
        executor.shutdown()
        if executor_thread is not None:
            executor_thread.join(timeout=5.0)
        for node in nodes:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
