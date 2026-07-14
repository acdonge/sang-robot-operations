"""Pure-Python recorder finite state machine and commit boundary."""

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Protocol
import uuid

from recorder_core.manifest import Manifest


class RecorderState(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    WARMUP = "WARMUP"
    RECORDING = "RECORDING"
    RESETTING = "RESETTING"
    COMMITTING = "COMMITTING"
    READY = "READY"
    ABORTING = "ABORTING"
    FAULT = "FAULT"


ALLOWED_TRANSITIONS = {
    RecorderState.IDLE: frozenset({RecorderState.PREFLIGHT}),
    RecorderState.PREFLIGHT: frozenset(
        {RecorderState.WARMUP, RecorderState.ABORTING}
    ),
    RecorderState.WARMUP: frozenset(
        {RecorderState.RECORDING, RecorderState.ABORTING}
    ),
    RecorderState.RECORDING: frozenset(
        {RecorderState.RESETTING, RecorderState.COMMITTING, RecorderState.ABORTING}
    ),
    RecorderState.RESETTING: frozenset(
        {RecorderState.WARMUP, RecorderState.COMMITTING, RecorderState.ABORTING}
    ),
    RecorderState.COMMITTING: frozenset(
        {RecorderState.READY, RecorderState.ABORTING}
    ),
    RecorderState.READY: frozenset({RecorderState.IDLE}),
    RecorderState.ABORTING: frozenset({RecorderState.FAULT}),
    RecorderState.FAULT: frozenset({RecorderState.IDLE}),
}


@dataclass(frozen=True)
class Transition:
    previous: RecorderState
    current: RecorderState
    reason: str | None
    monotonic_time: float


class FrameSampleError(RuntimeError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(reason)
        self.source = source
        self.reason = reason


class RecorderWriter(Protocol):
    staging_path: Path
    frame_count: int

    def add_frame(self, frame: Mapping[str, Any], *, task: str) -> None: ...

    def save_episode(self) -> None: ...

    def discard_episode(self) -> None: ...

    def finalize(self) -> None: ...


WriterFactory = Callable[[str, str], RecorderWriter]
SampleSupplier = Callable[[], Mapping[str, Any]]
GateChecker = Callable[[Mapping[str, Any]], tuple[str | None, str | None]]
WarmupReady = Callable[[], bool]
ValidationRunner = Callable[[Path], None]
TransitionCallback = Callable[[Transition], None]
FrameTimingCallback = Callable[[float], None]


_TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _default_disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


class RecorderFSM:
    """Dependency-injected one-session recorder FSM, independent of ROS/LeRobot."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        manifest_path: str | Path,
        writer_factory: WriterFactory,
        sample_supplier: SampleSupplier,
        gate_checker: GateChecker,
        validation_runner: ValidationRunner,
        warmup_ready: WarmupReady | None = None,
        warmup_s: float = 1.0,
        min_free_gb: float = 1.0,
        disk_free_gb: Callable[[Path], float] = _default_disk_free_gb,
        transition_callback: TransitionCallback | None = None,
        frame_timing_callback: FrameTimingCallback | None = None,
        lerobot_sha: str = "unknown",
        sessions_root: str | Path | None = None,
    ) -> None:
        if not math.isfinite(warmup_s) or warmup_s < 0:
            raise ValueError("warmup_s must be finite and non-negative")
        if not math.isfinite(min_free_gb) or min_free_gb < 0:
            raise ValueError("min_free_gb must be finite and non-negative")
        self.manifest = manifest
        self.manifest_path = Path(manifest_path)
        self.writer_factory = writer_factory
        self.sample_supplier = sample_supplier
        self.gate_checker = gate_checker
        self.warmup_ready = warmup_ready
        self.validation_runner = validation_runner
        self.warmup_s = warmup_s
        self.min_free_gb = min_free_gb
        self.disk_free_gb = disk_free_gb
        self.transition_callback = transition_callback
        self.frame_timing_callback = frame_timing_callback
        self.lerobot_sha = lerobot_sha
        self.sessions_root = (
            Path(sessions_root)
            if sessions_root is not None
            else Path(manifest.recording.staging_root).parent / "sessions"
        )

        self._lock = threading.RLock()
        self.state = RecorderState.IDLE
        self.state_since = time.monotonic()
        self._last_clock_now = self.state_since
        self.session_id: str | None = None
        self.dataset_id: str | None = None
        self.task: str | None = None
        self.writer: RecorderWriter | None = None
        self.staging_path: Path | None = None
        self.raw_path: Path | None = None
        self.failure_source: str | None = None
        self.failure_reason: str | None = None
        self.cleanup_errors: list[str] = []
        self._recorded_frame_count = 0
        self._episode_frame_count = 0
        self._saved_frame_count = 0
        self.episode_index = 0
        self.task_instructions: tuple[str, ...] = ()
        self.current_instruction: str | None = None
        self.episode_time_s = 0.0
        self.reset_time_s = 0.0
        self.num_episodes = 0
        self.rosbag = False
        self.episodes: list[dict[str, Any]] = []
        self._warmup_started: float | None = None
        self._episode_started: float | None = None
        self._reset_started: float | None = None
        self._reset_operation: str | None = None
        self._continue_requested = False
        self._pending_episode: dict[str, Any] | None = None
        self._transitions: list[Transition] = []
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="recorder-writer"
        )
        self._worker_future: Future[Any] | None = None
        self._cancel_requested = threading.Event()
        self._promotion_done = threading.Event()
        self._last_add_frame_ms: float | None = None
        self._max_add_frame_ms = 0.0
        self._add_frame_over_50ms = 0

    @property
    def frame_count(self) -> int:
        return (
            self._recorded_frame_count
            if self.writer is None
            else self._episode_frame_count
        )

    @property
    def reset_remaining_s(self) -> float | None:
        if self.state is not RecorderState.RESETTING:
            return None
        if self._worker_future is not None or self._reset_started is None:
            return self.reset_time_s
        return max(
            0.0,
            self.reset_time_s - (self._last_clock_now - self._reset_started),
        )

    @property
    def episodes_saved(self) -> int:
        return sum(episode["result"] == "SAVED" for episode in self.episodes)

    def _transition(
        self, state: RecorderState, reason: str | None = None, *, now: float | None = None
    ) -> None:
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise RuntimeError(
                f"invalid recorder transition: {self.state.value} -> {state.value}"
            )
        timestamp = time.monotonic() if now is None else now
        event = Transition(self.state, state, reason, timestamp)
        self.state = state
        self.state_since = timestamp
        self._last_clock_now = timestamp
        self._transitions.append(event)
        if self.transition_callback is not None:
            try:
                self.transition_callback(event)
            except Exception:
                pass

    @staticmethod
    def _dataset_id(manifest: Manifest, task: str, now_utc: datetime) -> str:
        timestamp = now_utc.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return (
            f"{manifest.recording.contract}__{task}__"
            f"{manifest.robot.real_or_sim}__{timestamp}"
        )

    def _checked_sample(self) -> Mapping[str, Any]:
        sample = self.sample_supplier()
        source, reason = self.gate_checker(sample)
        if source is not None:
            raise FrameSampleError(source, reason or "frame gate failed")
        return sample

    def _submit_worker(self, function: Callable[..., Any], *args) -> None:
        self._worker_future = self._worker.submit(function, *args)

    def _create_writer_worker(
        self, session_id: str, dataset_id: str, staging_root: Path
    ) -> RecorderWriter:
        staging_root.mkdir(parents=True, exist_ok=True)
        free_gb = self.disk_free_gb(staging_root)
        if free_gb < self.min_free_gb:
            raise FrameSampleError(
                "disk",
                f"staging free space {free_gb:.3f} GiB is below "
                f"minimum {self.min_free_gb:.3f} GiB",
            )
        return self.writer_factory(session_id, dataset_id)

    def _raw_dataset_path(self) -> Path:
        assert self.dataset_id is not None
        assert self.task is not None
        return (
            Path(self.manifest.recording.dataset_root)
            / self.manifest.recording.contract
            / self.task
            / self.manifest.robot.real_or_sim
            / self.dataset_id
        )

    def _save_episode_worker(self, writer: RecorderWriter) -> None:
        writer.save_episode()

    def _discard_episode_worker(self, writer: RecorderWriter) -> None:
        writer.discard_episode()

    def _complete_pending_episode_locked(self, result: str) -> None:
        pending = self._pending_episode
        if pending is None:
            raise RuntimeError("episode operation has no pending metadata")
        entry = {**pending, "result": result}
        self.episodes.append(entry)
        if result == "SAVED":
            self._saved_frame_count += int(entry["frames"])
            self.episode_index += 1
            self.current_instruction = self.task_instructions[
                self.episode_index % len(self.task_instructions)
            ]
        self._episode_frame_count = 0
        self._pending_episode = None

    def _commit_worker(
        self,
        writer: RecorderWriter,
        staging_path: Path,
        raw_path: Path,
        save_current: bool,
    ) -> Path:
        promoted = False
        try:
            if save_current:
                writer.save_episode()
                with self._lock:
                    self._complete_pending_episode_locked("SAVED")
            if self._cancel_requested.is_set():
                raise RuntimeError("commit cancelled by abort request")
            writer.finalize()
            if self._cancel_requested.is_set():
                raise RuntimeError("commit cancelled by abort request")
            self.validation_runner(staging_path)
            if self._cancel_requested.is_set():
                raise RuntimeError("commit cancelled by abort request")

            raw_path.parent.mkdir(parents=True, exist_ok=True)
            if raw_path.exists():
                raise RuntimeError(f"raw dataset path already exists: {raw_path}")
            staging_path.rename(raw_path)
            promoted = True
            self._promotion_done.set()
            if self._cancel_requested.is_set():
                raise RuntimeError("commit cancelled by abort request")
            self._write_session_log("PASS", raw_path=raw_path)
            if self._cancel_requested.is_set():
                raise RuntimeError("commit cancelled by abort request")
            return raw_path
        except Exception:
            if promoted and raw_path.exists():
                try:
                    raw_path.rename(staging_path)
                except OSError as cleanup_exc:
                    try:
                        shutil.rmtree(raw_path)
                    except OSError as delete_exc:
                        raise RuntimeError(
                            "promoted dataset rollback failed: "
                            f"rename={cleanup_exc}; delete={delete_exc}"
                        ) from delete_exc
                finally:
                    if not raw_path.exists():
                        self._promotion_done.clear()
            raise

    def _abort_cleanup_worker(
        self, staging_path: Path | None, raw_path: Path | None
    ) -> list[str]:
        cleanup_errors: list[str] = []
        if raw_path is not None and self._promotion_done.is_set() and raw_path.exists():
            try:
                shutil.rmtree(raw_path)
                self._promotion_done.clear()
            except OSError as exc:
                cleanup_errors.append(f"promoted dataset cleanup failed: {exc}")
        if staging_path is not None and staging_path.exists():
            try:
                shutil.rmtree(staging_path)
            except OSError as exc:
                cleanup_errors.append(f"staging cleanup failed: {exc}")
        try:
            self._write_session_log(
                "FAIL", raw_path=None, cleanup_errors=cleanup_errors
            )
        except Exception as exc:
            cleanup_errors.append(f"session log write failed: {exc}")
        return cleanup_errors

    def start(
        self,
        task: str,
        *,
        task_instructions: list[str] | tuple[str, ...] | None = None,
        episode_time_s: float = 0.0,
        reset_time_s: float = 0.0,
        num_episodes: int = 0,
        rosbag: bool = False,
        session_id: str | None = None,
        now_monotonic: float | None = None,
        now_utc: datetime | None = None,
    ) -> bool:
        """Enter PREFLIGHT and create the writer asynchronously."""
        with self._lock:
            if self.state is not RecorderState.IDLE:
                raise RuntimeError(f"record_start requires IDLE, got {self.state.value}")
            if not _TASK_PATTERN.fullmatch(task):
                raise ValueError(
                    "task must be a path-safe slug containing letters, digits, '.', '_' or '-'"
                )
            if task_instructions is not None and not isinstance(
                task_instructions, (list, tuple)
            ):
                raise ValueError("task_instructions must be a list of strings")
            instructions = tuple(
                instruction.strip()
                for instruction in (task_instructions or (task,))
                if isinstance(instruction, str) and instruction.strip()
            )
            if not instructions or (
                task_instructions
                and len(instructions) != len(task_instructions)
            ):
                raise ValueError("task_instructions must contain only non-empty strings")
            if (
                isinstance(episode_time_s, bool)
                or not isinstance(episode_time_s, (int, float))
                or not math.isfinite(episode_time_s)
                or episode_time_s < 0
            ):
                raise ValueError("episode_time_s must be finite and non-negative")
            if (
                isinstance(reset_time_s, bool)
                or not isinstance(reset_time_s, (int, float))
                or not math.isfinite(reset_time_s)
                or reset_time_s < 0
            ):
                raise ValueError("reset_time_s must be finite and non-negative")
            if (
                isinstance(num_episodes, bool)
                or not isinstance(num_episodes, int)
                or num_episodes < 0
            ):
                raise ValueError("num_episodes must be a non-negative integer")
            if not isinstance(rosbag, bool):
                raise ValueError("rosbag must be a boolean")
            now = time.monotonic() if now_monotonic is None else now_monotonic
            self._last_clock_now = now
            utc_now = datetime.now(timezone.utc) if now_utc is None else now_utc
            self.session_id = session_id or uuid.uuid4().hex
            self.dataset_id = self._dataset_id(self.manifest, task, utc_now)
            self.task = task
            self.task_instructions = instructions
            self.current_instruction = instructions[0]
            self.episode_time_s = float(episode_time_s)
            self.reset_time_s = float(reset_time_s)
            self.num_episodes = num_episodes
            self.rosbag = rosbag
            self.episode_index = 0
            self.episodes = []
            self.failure_source = None
            self.failure_reason = None
            self.cleanup_errors = []
            self._recorded_frame_count = 0
            self._episode_frame_count = 0
            self._saved_frame_count = 0
            self.raw_path = None
            self.writer = None
            self.staging_path = (
                Path(self.manifest.recording.staging_root)
                / f"{self.session_id}.partial"
            )
            self._warmup_started = None
            self._episode_started = None
            self._reset_started = None
            self._reset_operation = None
            self._continue_requested = False
            self._pending_episode = None
            self._transitions = []
            self._cancel_requested.clear()
            self._promotion_done.clear()
            self._transition(RecorderState.PREFLIGHT, now=now)

            try:
                self._checked_sample()
            except FrameSampleError as exc:
                self._begin_abort_locked(exc.reason, source=exc.source, now=now)
                return False
            except Exception as exc:
                self._begin_abort_locked(str(exc), source="preflight", now=now)
                return False

            self._submit_worker(
                self._create_writer_worker,
                self.session_id,
                self.dataset_id,
                Path(self.manifest.recording.staging_root),
            )
            return True

    def tick(self, *, now_monotonic: float | None = None) -> None:
        """Poll worker work, advance WARMUP, or record one gated sample."""
        with self._lock:
            now = time.monotonic() if now_monotonic is None else now_monotonic
            self._last_clock_now = now
            if self.state is RecorderState.PREFLIGHT:
                self._poll_preflight_locked(now)
                return
            if self.state is RecorderState.RESETTING:
                self._poll_resetting_locked(now)
                return
            if self.state is RecorderState.COMMITTING:
                self._poll_commit_locked(now)
                return
            if self.state is RecorderState.ABORTING:
                self._poll_abort_locked(now)
                return
            if self.state not in (RecorderState.WARMUP, RecorderState.RECORDING):
                return
            try:
                if (
                    self.state is RecorderState.WARMUP
                    and self.warmup_ready is not None
                    and not self.warmup_ready()
                ):
                    self._warmup_started = None
                    return
                sample = self._checked_sample()
                if self.state is RecorderState.WARMUP:
                    if self._warmup_started is None:
                        self._warmup_started = now
                    if now - self._warmup_started >= self.warmup_s:
                        self._transition(RecorderState.RECORDING, now=now)
                        self._episode_started = now
                    return
                if self.writer is None or self.current_instruction is None:
                    raise RuntimeError("recording state has no active writer/instruction")
                started = time.perf_counter()
                try:
                    self.writer.add_frame(sample, task=self.current_instruction)
                finally:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    self._last_add_frame_ms = elapsed_ms
                    self._max_add_frame_ms = max(self._max_add_frame_ms, elapsed_ms)
                    if elapsed_ms > 50.0:
                        self._add_frame_over_50ms += 1
                    if self.frame_timing_callback is not None:
                        try:
                            self.frame_timing_callback(elapsed_ms)
                        except Exception:
                            pass
                self._episode_frame_count += 1
                if (
                    self.episode_time_s > 0
                    and self._episode_started is not None
                    and now - self._episode_started >= self.episode_time_s
                ):
                    self._begin_save_next_locked(now, "episode time limit reached")
            except FrameSampleError as exc:
                self._begin_abort_locked(exc.reason, source=exc.source, now=now)
            except Exception as exc:
                self._begin_abort_locked(str(exc), source="sampler", now=now)

    def _poll_preflight_locked(self, now: float) -> None:
        future = self._worker_future
        if future is None or not future.done():
            return
        self._worker_future = None
        try:
            writer = future.result()
        except FrameSampleError as exc:
            self._begin_abort_locked(exc.reason, source=exc.source, now=now)
            return
        except Exception as exc:
            self._begin_abort_locked(str(exc), source="preflight", now=now)
            return
        self.writer = writer
        self.staging_path = Path(writer.staging_path)
        self._warmup_started = now if self.warmup_ready is None else None
        try:
            self._write_session_log("IN_PROGRESS")
        except Exception as exc:
            self._begin_abort_locked(str(exc), source="session", now=now)
            return
        self._transition(RecorderState.WARMUP, now=now)

    def _poll_resetting_locked(self, now: float) -> None:
        future = self._worker_future
        if future is not None:
            if not future.done():
                return
            self._worker_future = None
            operation = self._reset_operation
            try:
                future.result()
                self._complete_pending_episode_locked(
                    "SAVED" if operation == "save" else "RETRIED"
                )
                self._write_session_log("IN_PROGRESS")
            except Exception as exc:
                self._begin_abort_locked(str(exc), source=operation or "reset", now=now)
                return
            self._reset_operation = None
            if self.num_episodes and self.episodes_saved >= self.num_episodes:
                self._begin_commit_locked(
                    now,
                    save_current=False,
                    reason="configured episode count reached",
                )
                return
            self._reset_started = now

        if self._reset_started is None:
            return
        if self._continue_requested or now - self._reset_started >= self.reset_time_s:
            self._continue_requested = False
            self._reset_started = None
            self._warmup_started = now if self.warmup_ready is None else None
            self._transition(RecorderState.WARMUP, "reset complete", now=now)

    def _poll_commit_locked(self, now: float) -> None:
        future = self._worker_future
        if future is None or not future.done():
            return
        self._worker_future = None
        try:
            self.raw_path = Path(future.result())
        except Exception as exc:
            self.raw_path = None
            self._begin_abort_locked(str(exc), source="commit", now=now)
            return
        self._transition(RecorderState.READY, now=now)

    def _poll_abort_locked(self, now: float) -> None:
        future = self._worker_future
        if future is None or not future.done():
            return
        self._worker_future = None
        try:
            self.cleanup_errors.extend(future.result())
        except Exception as exc:
            self.cleanup_errors.append(f"abort worker failed: {exc}")
        self._transition(RecorderState.FAULT, self.failure_reason, now=now)

    def _pending_episode_metadata(self, reason: str) -> dict[str, Any]:
        if self.current_instruction is None:
            raise RuntimeError("session has no current instruction")
        return {
            "index": self.episode_index,
            "instruction": self.current_instruction,
            "frames": self._episode_frame_count,
            "reason": reason,
        }

    def _begin_commit_locked(
        self, now: float, *, save_current: bool, reason: str | None = None
    ) -> None:
        self._transition(RecorderState.COMMITTING, reason, now=now)
        if self.writer is None or self.staging_path is None:
            raise RuntimeError("commit has no active writer/staging path")
        self._submit_worker(
            self._commit_worker,
            self.writer,
            self.staging_path,
            self._raw_dataset_path(),
            save_current,
        )

    def _begin_save_next_locked(self, now: float, reason: str) -> None:
        if self.writer is None:
            raise RuntimeError("save_and_next has no active writer")
        if self._episode_frame_count <= 0:
            raise RuntimeError("cannot save an empty episode")
        self._pending_episode = self._pending_episode_metadata(reason)
        self._reset_operation = "save"
        self._reset_started = None
        self._continue_requested = False
        self._episode_started = None
        self._transition(RecorderState.RESETTING, reason, now=now)
        self._submit_worker(self._save_episode_worker, self.writer)

    def save_and_next(self, *, now_monotonic: float | None = None) -> bool:
        """Save the current episode, then reset or auto-finish the session."""
        with self._lock:
            if self.state is not RecorderState.RECORDING:
                raise RuntimeError(
                    f"save_and_next requires RECORDING, got {self.state.value}"
                )
            now = time.monotonic() if now_monotonic is None else now_monotonic
            self._begin_save_next_locked(now, "save_and_next")
            return True

    def retry(self, *, now_monotonic: float | None = None) -> bool:
        """Discard the current episode buffer and reset without advancing."""
        with self._lock:
            if self.state is not RecorderState.RECORDING:
                raise RuntimeError(f"retry requires RECORDING, got {self.state.value}")
            if self.writer is None:
                raise RuntimeError("retry has no active writer")
            now = time.monotonic() if now_monotonic is None else now_monotonic
            self._pending_episode = self._pending_episode_metadata("retry")
            self._reset_operation = "retry"
            self._reset_started = None
            self._continue_requested = False
            self._episode_started = None
            self._transition(RecorderState.RESETTING, "retry", now=now)
            self._submit_worker(self._discard_episode_worker, self.writer)
            return True

    def continue_after_reset(self) -> bool:
        """Request early reset completion, including while save/discard is pending."""
        with self._lock:
            if self.state is not RecorderState.RESETTING:
                raise RuntimeError(
                    f"continue requires RESETTING, got {self.state.value}"
                )
            self._continue_requested = True
            return True

    def finish(self, *, now_monotonic: float | None = None) -> bool:
        """Save the current episode, finalize, validate, and promote asynchronously."""
        with self._lock:
            if self.state is not RecorderState.RECORDING:
                raise RuntimeError(f"finish requires RECORDING, got {self.state.value}")
            if self._episode_frame_count <= 0:
                raise RuntimeError("cannot finish with an empty episode")
            now = time.monotonic() if now_monotonic is None else now_monotonic
            self._pending_episode = self._pending_episode_metadata("finish")
            self._episode_started = None
            try:
                self._begin_commit_locked(now, save_current=True, reason="finish")
            except Exception as exc:
                self._begin_abort_locked(str(exc), source="commit", now=now)
                return False
            return True

    def stop(self, *, now_monotonic: float | None = None) -> bool:
        """Backward-compatible alias for :meth:`finish`."""
        return self.finish(now_monotonic=now_monotonic)

    def abort(self, reason: str = "explicit abort") -> bool:
        with self._lock:
            if self.state not in (
                RecorderState.PREFLIGHT,
                RecorderState.WARMUP,
                RecorderState.RECORDING,
                RecorderState.RESETTING,
                RecorderState.COMMITTING,
            ):
                return False
            self._begin_abort_locked(reason, source="operator")
            return True

    def _begin_abort_locked(
        self, reason: str, *, source: str, now: float | None = None
    ) -> None:
        if self.state in (RecorderState.ABORTING, RecorderState.FAULT):
            return
        self.failure_source = self.failure_source or source
        self.failure_reason = self.failure_reason or reason
        self._cancel_requested.set()
        self._transition(RecorderState.ABORTING, self.failure_reason, now=now)

        if self._pending_episode is not None:
            self.episodes.append({**self._pending_episode, "result": "ABORTED"})
            self._pending_episode = None

        staging_path = self.staging_path
        if staging_path is None and self.session_id is not None:
            staging_path = (
                Path(self.manifest.recording.staging_root)
                / f"{self.session_id}.partial"
            )
        self._recorded_frame_count = self._saved_frame_count + self._episode_frame_count
        self.writer = None
        raw_path = self._raw_dataset_path() if self.dataset_id and self.task else None
        self.raw_path = None
        self._submit_worker(self._abort_cleanup_worker, staging_path, raw_path)

    def _write_session_log(
        self,
        result: str,
        *,
        raw_path: Path | None = None,
        cleanup_errors: list[str] | None = None,
    ) -> None:
        if self.session_id is None:
            return
        session_root = self.sessions_root / self.session_id
        session_root.mkdir(parents=True, exist_ok=True)
        manifest_copy = session_root / "manifest.yaml"
        if not manifest_copy.exists():
            shutil.copy2(self.manifest_path, manifest_copy)
        payload = {
            "session_id": self.session_id,
            "dataset_id": self.dataset_id,
            "task": self.task,
            "task_instructions": list(self.task_instructions),
            "episode_time_s": self.episode_time_s,
            "reset_time_s": self.reset_time_s,
            "num_episodes": self.num_episodes,
            "rosbag": self.rosbag,
            "contract": self.manifest.recording.contract,
            "domain": self.manifest.robot.real_or_sim,
            "lerobot_sha": self.lerobot_sha,
            "manifest_copy": manifest_copy.name,
            "frame_count": self._saved_frame_count + self._episode_frame_count,
            "episodes": list(self.episodes),
            "result": result,
            "raw_path": None if raw_path is None else str(raw_path),
            "failure_source": self.failure_source,
            "failure_reason": self.failure_reason,
            "cleanup_errors": list(
                self.cleanup_errors if cleanup_errors is None else cleanup_errors
            ),
            "transitions": [
                {
                    "from": transition.previous.value,
                    "to": transition.current.value,
                    "reason": transition.reason,
                }
                for transition in self._transitions
            ],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        output = session_root / "session.json"
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(output)

    def reset(self) -> None:
        """Explicitly release READY/FAULT latch; every new start re-runs PREFLIGHT."""
        with self._lock:
            if self.state not in (RecorderState.READY, RecorderState.FAULT):
                raise RuntimeError(f"reset requires READY or FAULT, got {self.state.value}")
            self.writer = None
            self.staging_path = None
            self.raw_path = None
            self._warmup_started = None
            self._episode_started = None
            self._reset_started = None
            self._reset_operation = None
            self._continue_requested = False
            self._pending_episode = None
            self._cancel_requested.clear()
            self._promotion_done.clear()
            self._transition(RecorderState.IDLE, "explicit reset")

    def get_frame_write_health(self) -> dict[str, float | int | None]:
        with self._lock:
            return {
                "last_tick_ms": self._last_add_frame_ms,
                "max_tick_ms": self._max_add_frame_ms,
                "ticks_over_50ms": self._add_frame_over_50ms,
            }

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            elapsed_s = (
                0.0
                if self._episode_started is None
                else max(0.0, self._last_clock_now - self._episode_started)
            )
            return {
                "state": self.state.value,
                "phase": self.state.value,
                "session_id": self.session_id,
                "dataset_id": self.dataset_id,
                "task": self.task,
                "frame_count": self.frame_count,
                "episode_index": self.episode_index,
                "episodes_total": self.num_episodes,
                "episodes_saved": self.episodes_saved,
                "current_instruction": self.current_instruction,
                "episode_elapsed_s": elapsed_s,
                "reset_remaining_s": self.reset_remaining_s,
                "staging_path": None
                if self.staging_path is None
                else str(self.staging_path),
                "raw_path": None if self.raw_path is None else str(self.raw_path),
                "failure_source": self.failure_source,
                "failure_reason": self.failure_reason,
                "cleanup_errors": list(self.cleanup_errors),
                "add_frame": self.get_frame_write_health(),
            }

    def close(self) -> None:
        """Let queued worker work drain without blocking the caller."""
        self._worker.shutdown(wait=False)
