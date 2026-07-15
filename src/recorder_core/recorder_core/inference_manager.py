"""Closed-loop LeRobot executor for fake_robot and simulation only.

This executor is not a real-robot safety boundary. Connecting it to a physical
robot bridge requires the M7 safety gate (including the bridge deadman) first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Any

import cv2
import numpy as np

from recorder_core.gate import check_frame
from recorder_core.manifest import Manifest


ACTIVE_STATES = frozenset({"LOADING", "RUNNING"})
SAFETY_WARNING = (
    "fake_robot/sim only; real-robot bridge use requires the M7 safety gate "
    "(bridge deadman) first"
)


class InferencePreflightError(RuntimeError):
    """Raised when a policy cannot be safely started for a manifest."""


class InferenceConflictError(RuntimeError):
    """Raised when the single-active-inference contract would be violated."""


@dataclass(frozen=True)
class PolicyRuntime:
    policy: Any
    preprocessor: Callable[[dict[str, Any]], dict[str, Any]]
    postprocessor: Callable[[Any], Any]
    torch: Any


def load_policy_runtime(checkpoint_path: str | Path) -> PolicyRuntime:
    """Load a local v0.6 checkpoint through LeRobot's public policy APIs."""
    try:
        import torch
        from lerobot.configs import PreTrainedConfig
        from lerobot.policies import get_policy_class, make_pre_post_processors
    except ImportError as exc:
        raise InferencePreflightError(
            "LeRobot is unavailable; run inference in the pinned dataset container"
        ) from exc

    checkpoint = Path(checkpoint_path)
    try:
        config = PreTrainedConfig.from_pretrained(
            checkpoint,
            local_files_only=True,
        )
        config.device = "cpu"
        config.pretrained_path = checkpoint
        policy_class = get_policy_class(config.type)
        policy = policy_class.from_pretrained(
            checkpoint,
            config=config,
            local_files_only=True,
        )
        policy.to("cpu")
        policy.eval()
        policy.reset()
        preprocessor, postprocessor = make_pre_post_processors(
            policy_cfg=policy.config,
            pretrained_path=str(checkpoint),
            preprocessor_overrides={
                "device_processor": {"device": "cpu"},
            },
            postprocessor_overrides={
                "device_processor": {"device": "cpu"},
            },
        )
    except Exception as exc:
        raise InferencePreflightError(f"policy load failed: {exc}") from exc
    return PolicyRuntime(policy, preprocessor, postprocessor, torch)


def _feature_shape(feature: Any) -> tuple[int, ...]:
    value = (
        feature.get("shape")
        if isinstance(feature, Mapping)
        else getattr(feature, "shape", None)
    )
    if not isinstance(value, (tuple, list)) or any(
        isinstance(size, bool) or not isinstance(size, int) or size <= 0
        for size in value
    ):
        raise InferencePreflightError(f"policy feature has invalid shape: {value!r}")
    return tuple(value)


def manifest_policy_features(
    manifest: Manifest,
) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    """Return the exact channel-first policy feature contract for a manifest."""
    inputs = {
        interface.key: interface.shape for interface in manifest.interfaces.state
    }
    inputs.update(
        {
            f"observation.images.{camera.key}": (
                3,
                camera.height,
                camera.width,
            )
            for camera in manifest.interfaces.cameras
        }
    )
    outputs = {
        interface.key: interface.shape for interface in manifest.interfaces.action
    }
    return inputs, outputs


def preflight_policy_schema(policy: Any, manifest: Manifest) -> None:
    """Require exact input/output feature keys and shapes before execution."""
    config = getattr(policy, "config", None)
    policy_inputs = getattr(config, "input_features", None)
    policy_outputs = getattr(config, "output_features", None)
    if not isinstance(policy_inputs, Mapping) or not isinstance(policy_outputs, Mapping):
        raise InferencePreflightError(
            "policy config must expose input_features and output_features mappings"
        )

    expected_inputs, expected_outputs = manifest_policy_features(manifest)
    problems = []
    for label, actual, expected in (
        ("input", policy_inputs, expected_inputs),
        ("output", policy_outputs, expected_outputs),
    ):
        actual_keys = set(actual)
        expected_keys = set(expected)
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        if missing:
            problems.append(f"{label} missing keys={missing}")
        if extra:
            problems.append(f"{label} extra keys={extra}")
        for key in sorted(actual_keys & expected_keys):
            actual_shape = _feature_shape(actual[key])
            if actual_shape != expected[key]:
                problems.append(
                    f"{label} {key!r} shape={actual_shape}, expected={expected[key]}"
                )
    if problems:
        raise InferencePreflightError("policy/manifest schema mismatch: " + "; ".join(problems))


def final_checkpoint(run_path: str | Path) -> Path | None:
    """Resolve the last LeRobot pretrained_model directory for a run."""
    checkpoints = Path(run_path) / "output" / "checkpoints"
    preferred = checkpoints / "last" / "pretrained_model"
    if _checkpoint_has_files(preferred):
        return preferred.resolve()
    try:
        numbered = sorted(
            (
                path
                for path in checkpoints.iterdir()
                if path.is_dir() and path.name.isdigit()
            ),
            key=lambda path: int(path.name),
            reverse=True,
        )
    except OSError:
        return None
    for checkpoint in numbered:
        candidate = checkpoint / "pretrained_model"
        if _checkpoint_has_files(candidate):
            return candidate.resolve()
    return None


def _checkpoint_has_files(path: Path) -> bool:
    try:
        return path.is_dir() and any(item.is_file() for item in path.iterdir())
    except OSError:
        return False


def _jpeg_rgb_tensor(value: Any, camera_key: str, camera: Any, torch: Any) -> Any:
    payload = getattr(value, "data", value)
    try:
        encoded = np.frombuffer(bytes(payload), dtype=np.uint8)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"camera {camera_key!r} does not contain JPEG bytes") from exc
    bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"camera {camera_key!r} JPEG decode failed")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    expected = (camera.height, camera.width, 3)
    if rgb.shape != expected:
        raise RuntimeError(
            f"camera {camera_key!r} shape mismatch: got {rgb.shape}, expected {expected}"
        )
    chw = np.ascontiguousarray(rgb.transpose(2, 0, 1))
    return torch.from_numpy(chw).to(dtype=torch.float32).div_(255.0)


def build_policy_observation(
    sample: Mapping[str, Any], manifest: Manifest, torch: Any
) -> dict[str, Any]:
    observation = {}
    for interface in manifest.interfaces.state:
        vector = np.asarray(sample[interface.key], dtype=np.float32)
        observation[interface.key] = torch.from_numpy(vector.copy())
    for camera in manifest.interfaces.cameras:
        observation[f"observation.images.{camera.key}"] = _jpeg_rgb_tensor(
            sample[camera.key], camera.key, camera, torch
        )
    return observation


class InferenceManager:
    """Own exactly one fake_robot/sim policy execution loop."""

    def __init__(
        self,
        store_root: str | Path,
        *,
        run_supplier: Callable[[], list[dict[str, Any]]] = lambda: [],
        policy_loader: Callable[[str | Path], PolicyRuntime] = load_policy_runtime,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store_root = Path(store_root)
        self.runs_root = self.store_root / "runs"
        self._run_supplier = run_supplier
        self._policy_loader = policy_loader
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._state = "STOPPED"
        self._run_id: str | None = None
        self._robot_id: str | None = None
        self._detail = "not running"
        self._runtime: PolicyRuntime | None = None
        self._node: Any = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._started_monotonic: float | None = None
        self._last_action_monotonic: float | None = None
        self._action_count = 0

    def list_policies(self) -> list[dict[str, Any]]:
        policies = []
        seen: set[str] = set()
        for run in self._run_supplier():
            run_id = run.get("run_id")
            if run.get("state") != "DONE" or not isinstance(run_id, str):
                continue
            if not run_id or Path(run_id).name != run_id:
                continue
            checkpoint = final_checkpoint(self.runs_root / run_id)
            if checkpoint is None:
                continue
            seen.add(run_id)
            policies.append(
                {
                    "run_id": run_id,
                    "dataset_id": run.get("dataset_id"),
                    "policy_type": run.get("policy_type"),
                    "checkpoint_path": str(checkpoint),
                    "created_at": run.get("created_at"),
                }
            )
        # 이전 게이트웨이 세션에서 완료된 run은 메모리 registry에 없다 —
        # 디스크의 lineage.json + 최종 checkpoint를 근거로 병합한다.
        if self.runs_root.is_dir():
            for run_dir in sorted(self.runs_root.iterdir()):
                run_id = run_dir.name
                if not run_dir.is_dir() or run_id in seen:
                    continue
                checkpoint = final_checkpoint(run_dir)
                if checkpoint is None:
                    continue
                lineage: dict[str, Any] = {}
                lineage_path = run_dir / "lineage.json"
                if lineage_path.is_file():
                    try:
                        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        lineage = {}
                policies.append(
                    {
                        "run_id": run_id,
                        "dataset_id": lineage.get("dataset_id"),
                        "policy_type": lineage.get("policy_type"),
                        "checkpoint_path": str(checkpoint),
                        "created_at": lineage.get("created_at"),
                    }
                )
        return policies

    def _policy_for_run(self, run_id: str) -> dict[str, Any]:
        if not run_id or Path(run_id).name != run_id:
            raise InferencePreflightError("run_id must be one path component")
        for policy in self.list_policies():
            if policy["run_id"] == run_id:
                return policy
        raise InferencePreflightError(
            f"run is not DONE with a final checkpoint: {run_id}"
        )

    def status(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            started = self._started_monotonic
            last_action = self._last_action_monotonic
            count = self._action_count
            elapsed = None if started is None else max(0.0, now - started)
            hz = 0.0 if not elapsed else count / elapsed
            action_age = (
                None
                if last_action is None
                else max(0.0, (now - last_action) * 1000.0)
            )
            return {
                "type": "inference",
                "state": self._state,
                "run_id": self._run_id,
                "robot_id": self._robot_id,
                "hz": round(hz, 3),
                "last_action_age_ms": (
                    None if action_age is None else round(action_age, 3)
                ),
                "detail": self._detail,
            }

    def start(self, run_id: str, node: Any) -> dict[str, Any]:
        robot_id = node.manifest.robot.robot_id
        with self._lock:
            if self._state in ACTIVE_STATES:
                if self._run_id == run_id and self._robot_id == robot_id:
                    return self.status_unlocked()
                raise InferenceConflictError("another inference is already active")
            self._generation += 1
            generation = self._generation
            self._state = "LOADING"
            self._run_id = run_id
            self._robot_id = robot_id
            self._detail = "loading final checkpoint"
            self._stop_event = threading.Event()
            stop_event = self._stop_event
            self._started_monotonic = None
            self._last_action_monotonic = None
            self._action_count = 0

        try:
            policy_record = self._policy_for_run(run_id)
            runtime = self._policy_loader(policy_record["checkpoint_path"])
            preflight_policy_schema(runtime.policy, node.manifest)
            runtime.policy.reset()
        except InferencePreflightError as exc:
            self._set_loading_error(generation, str(exc))
            raise
        except Exception as exc:
            detail = f"policy preflight failed: {exc}"
            self._set_loading_error(generation, detail)
            raise InferencePreflightError(detail) from exc

        with self._lock:
            if generation != self._generation or stop_event.is_set():
                return self.status_unlocked()
            self._runtime = runtime
            self._node = node
            self._started_monotonic = self._monotonic()
            self._state = "RUNNING"
            self._detail = "closed-loop inference running"
            thread = threading.Thread(
                target=self._run_loop,
                args=(generation, runtime, node, stop_event),
                name=f"inference-{robot_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self.status_unlocked()

    def status_unlocked(self) -> dict[str, Any]:
        """Build a status payload while ``self._lock`` is already held."""
        now = self._monotonic()
        elapsed = (
            None
            if self._started_monotonic is None
            else max(0.0, now - self._started_monotonic)
        )
        age = (
            None
            if self._last_action_monotonic is None
            else max(0.0, (now - self._last_action_monotonic) * 1000.0)
        )
        return {
            "type": "inference",
            "state": self._state,
            "run_id": self._run_id,
            "robot_id": self._robot_id,
            "hz": round(0.0 if not elapsed else self._action_count / elapsed, 3),
            "last_action_age_ms": None if age is None else round(age, 3),
            "detail": self._detail,
        }

    def _set_loading_error(self, generation: int, detail: str) -> None:
        with self._lock:
            if generation == self._generation and self._state == "LOADING":
                self._state = "ERROR"
                self._detail = detail

    @staticmethod
    def _observation_sources(manifest: Manifest) -> tuple[str, ...]:
        return tuple(
            interface.key for interface in manifest.interfaces.state
        ) + tuple(camera.key for camera in manifest.interfaces.cameras)

    def _fresh_sample(self, node: Any) -> Mapping[str, Any]:
        sample = node.get_inference_sample()
        source, reason = check_frame(
            node.manifest,
            source_ages_ms=sample["_source_ages_ms"],
            vectors=sample["_vectors"],
            cameras=sample["_cameras"],
            required_sources=self._observation_sources(node.manifest),
        )
        if source is not None:
            raise RuntimeError(f"observation gate failed: {source}: {reason}")
        return sample

    @staticmethod
    def _action_array(action: Any, manifest: Manifest) -> np.ndarray:
        if hasattr(action, "detach"):
            action = action.detach()
        if hasattr(action, "cpu"):
            action = action.cpu()
        if hasattr(action, "numpy"):
            action = action.numpy()
        vector = np.asarray(action, dtype=np.float32).reshape(-1)
        expected_dimension = sum(
            interface.dimension for interface in manifest.interfaces.action
        )
        if vector.shape != (expected_dimension,):
            raise RuntimeError(
                f"policy action shape mismatch: got {vector.shape}, "
                f"expected {(expected_dimension,)}"
            )
        if not np.all(np.isfinite(vector)):
            raise RuntimeError("policy action contains non-finite values")
        return vector

    def _run_loop(
        self,
        generation: int,
        runtime: PolicyRuntime,
        node: Any,
        stop_event: threading.Event,
    ) -> None:
        period = 1.0 / node.manifest.recording.fps
        deadline = self._monotonic()
        try:
            while not stop_event.is_set():
                sample = self._fresh_sample(node)
                observation = build_policy_observation(
                    sample, node.manifest, runtime.torch
                )
                processed = runtime.preprocessor(observation)
                with runtime.torch.inference_mode():
                    action = runtime.policy.select_action(processed)
                action = runtime.postprocessor(action)
                vector = self._action_array(action, node.manifest)

                # Re-check after inference so a slow policy never publishes an
                # action after its observation has crossed the freshness limit.
                self._fresh_sample(node)
                if stop_event.is_set():
                    break
                with self._lock:
                    if (
                        generation != self._generation
                        or self._state != "RUNNING"
                        or stop_event.is_set()
                    ):
                        break
                    # Serialize the final stop check with publication so no
                    # command can be emitted after stop() returns.
                    node.publish_policy_action(vector)
                    now = self._monotonic()
                    self._last_action_monotonic = now
                    self._action_count += 1

                deadline += period
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    deadline = self._monotonic()
                else:
                    stop_event.wait(remaining)
        except Exception as exc:
            with self._lock:
                if (
                    generation == self._generation
                    and self._state == "RUNNING"
                    and not stop_event.is_set()
                ):
                    self._state = "ERROR"
                    self._detail = str(exc)
                    stop_event.set()
        finally:
            with self._lock:
                if generation == self._generation:
                    self._runtime = None
                    self._node = None

    def stop(self) -> dict[str, Any]:
        """Stop command publication; repeated calls are successful no-ops."""
        with self._lock:
            if self._state == "STOPPED":
                return self.status_unlocked()
            self._generation += 1
            self._stop_event.set()
            thread = self._thread
            self._state = "STOPPED"
            self._detail = "inference stopped"
            self._runtime = None
            self._node = None
            payload = self.status_unlocked()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return payload
