"""Isolated LeRobot v0.6 training subprocess manager."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any

from recorder_core.writer import lerobot_revision


ACTIVE_STATES = frozenset({"PREPARING", "RUNNING", "STOPPING"})
TERMINAL_STATES = frozenset({"DONE", "ERROR"})
_UNSET = object()
_PROGRESS_RE = re.compile(
    r"\bstep:\s*([0-9][0-9,]*)\b.*?"
    r"\bloss:\s*([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)\b"
)


class TrainPreflightError(RuntimeError):
    """Raised before a training subprocess can safely start."""


class TrainConflictError(RuntimeError):
    """Raised when the global one-active-run contract would be violated."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def meta_digest(dataset_path: str | Path) -> str:
    """Hash all regular metadata files with their relative names."""
    meta_root = Path(dataset_path) / "meta"
    if not (meta_root / "info.json").is_file():
        raise TrainPreflightError("dataset meta/info.json is missing")
    digest = hashlib.sha256()
    try:
        paths = sorted(
            path
            for path in meta_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        for path in paths:
            relative = path.relative_to(meta_root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError as exc:
        raise TrainPreflightError(f"dataset metadata is unreadable: {exc}") from exc
    return digest.hexdigest()


def preflight_train(dataset_path: str | Path) -> Path:
    """Require a finalized, validated LeRobot dataset before training."""
    path = Path(dataset_path).resolve()
    if path.name.endswith(".partial"):
        raise TrainPreflightError("partial datasets cannot be trained")
    if not path.is_dir():
        raise TrainPreflightError(f"dataset path does not exist: {path}")
    try:
        validation = json.loads(
            (path / "validation.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise TrainPreflightError("validation.json is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainPreflightError("validation.json is unreadable") from exc
    if not isinstance(validation, dict) or validation.get("status") != "PASS":
        raise TrainPreflightError("dataset validation status is not PASS")
    meta_digest(path)
    return path


@dataclass
class _TrainRun:
    run_id: str
    dataset_id: str
    dataset_path: Path
    policy_type: str
    steps: int
    run_path: Path
    output_path: Path
    command: list[str]
    created_at: str
    state: str = "PREPARING"
    step: int | None = None
    loss: float | None = None
    detail: str = "preparing training subprocess"
    returncode: int | None = None
    finished_at: str | None = None
    process: Any = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    termination_started: bool = field(default=False, repr=False)


class TrainManager:
    """Own one active LeRobot CLI process and publish thread-safe progress events."""

    def __init__(
        self,
        store_root: str | Path,
        *,
        executable: str = "lerobot-train",
        popen_factory: Callable[..., Any] = subprocess.Popen,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        now: Callable[[], datetime] = _utc_now,
        revision: Callable[[], str] = lerobot_revision,
        stop_grace_s: float = 10.0,
    ) -> None:
        self.store_root = Path(store_root)
        self.runs_root = self.store_root / "runs"
        self._executable = executable
        self._popen_factory = popen_factory
        self._event_callback = event_callback
        self._now = now
        self._revision = revision
        self._stop_grace_s = stop_grace_s
        self._lock = threading.Lock()
        self._runs: dict[str, _TrainRun] = {}
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._event_sequence = 0

    @staticmethod
    def parse_progress(line: str) -> tuple[int, float] | None:
        """Parse the pinned v0.6 ``MetricsTracker`` log representation."""
        match = _PROGRESS_RE.search(line)
        if match is None:
            return None
        try:
            return int(match.group(1).replace(",", "")), float(match.group(2))
        except ValueError:
            return None

    def build_command(
        self,
        *,
        dataset_id: str,
        dataset_path: str | Path,
        policy_type: str,
        steps: int,
        output_path: str | Path,
        run_id: str,
    ) -> list[str]:
        """Build the offline-safe small ACT command used by the API and smoke test."""
        if policy_type != "act":
            raise TrainPreflightError(f"unsupported policy_type: {policy_type}")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise TrainPreflightError("steps must be a positive integer")
        return [
            self._executable,
            f"--dataset.repo_id={dataset_id}",
            f"--dataset.root={Path(dataset_path)}",
            "--policy.type=act",
            "--policy.device=cpu",
            "--policy.use_amp=false",
            "--policy.push_to_hub=false",
            "--policy.pretrained_backbone_weights=null",
            "--policy.dim_model=64",
            "--policy.n_heads=4",
            "--policy.dim_feedforward=128",
            "--policy.n_encoder_layers=1",
            "--policy.n_decoder_layers=1",
            "--policy.n_vae_encoder_layers=1",
            "--policy.latent_dim=16",
            "--policy.chunk_size=10",
            "--policy.n_action_steps=10",
            "--batch_size=1",
            "--num_workers=0",
            f"--steps={steps}",
            "--log_freq=1",
            "--save_checkpoint=true",
            f"--save_freq={steps}",
            "--wandb.enable=false",
            f"--output_dir={Path(output_path)}",
            f"--job_name={run_id}",
        ]

    def _publish(
        self,
        run: _TrainRun,
        *,
        state: str | object = _UNSET,
        step: int | None | object = _UNSET,
        loss: float | None | object = _UNSET,
        detail: str | object = _UNSET,
    ) -> None:
        with self._lock:
            if state is not _UNSET:
                run.state = str(state)
            if step is not _UNSET:
                run.step = step  # type: ignore[assignment]
            if loss is not _UNSET:
                run.loss = loss  # type: ignore[assignment]
            if detail is not _UNSET:
                run.detail = str(detail)
            self._event_sequence += 1
            event = {
                "type": "train",
                "run_id": run.run_id,
                "state": run.state,
                "step": run.step,
                "loss": run.loss,
            }
            self._events.append((self._event_sequence, event))
        if self._event_callback is not None:
            try:
                self._event_callback(dict(event))
            except Exception:
                pass

    def _run_payload(self, run: _TrainRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "dataset_id": run.dataset_id,
            "policy_type": run.policy_type,
            "steps": run.steps,
            "state": run.state,
            "step": run.step,
            "loss": run.loss,
            "path": str(run.run_path),
            "detail": run.detail,
            "returncode": run.returncode,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = [self._run_payload(run) for run in self._runs.values()]
        return sorted(runs, key=lambda item: item["created_at"], reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._run_payload(self._runs[run_id])

    def start_train(
        self,
        *,
        dataset_id: str,
        dataset_path: str | Path,
        policy_type: str,
        steps: int,
    ) -> str:
        """Write immutable lineage, then start a fresh non-resume training thread."""
        if not dataset_id or Path(dataset_id).name != dataset_id:
            raise TrainPreflightError("dataset_id must be one path component")
        path = preflight_train(dataset_path)
        created = self._now().astimezone(timezone.utc)
        timestamp = created.strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{dataset_id}__{policy_type}__{timestamp}"
        run_path = self.runs_root / run_id
        output_path = run_path / "output"
        command = self.build_command(
            dataset_id=dataset_id,
            dataset_path=path,
            policy_type=policy_type,
            steps=steps,
            output_path=output_path,
            run_id=run_id,
        )
        run = _TrainRun(
            run_id=run_id,
            dataset_id=dataset_id,
            dataset_path=path,
            policy_type=policy_type,
            steps=steps,
            run_path=run_path,
            output_path=output_path,
            command=command,
            created_at=created.isoformat(),
        )

        with self._lock:
            if any(current.state in ACTIVE_STATES for current in self._runs.values()):
                raise TrainConflictError("another training run is already active")
            if run_path.exists():
                raise TrainConflictError(f"training run path already exists: {run_path}")
            self._runs[run_id] = run
        self._publish(run, state="PREPARING", detail="writing immutable lineage")

        try:
            run_path.mkdir(parents=True, exist_ok=False)
            lineage = {
                "run_id": run_id,
                "created_at": run.created_at,
                "dataset": {
                    "dataset_id": dataset_id,
                    "path": str(path),
                    "meta_digest": {
                        "algorithm": "sha256",
                        "value": meta_digest(path),
                    },
                },
                "lerobot_revision": self._revision(),
                "policy_type": policy_type,
                "steps": steps,
                "command": command,
                "resume_supported": False,
            }
            lineage_path = run_path / "lineage.json"
            temporary = run_path / ".lineage.json.tmp"
            temporary.write_text(
                json.dumps(lineage, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o444)
            temporary.replace(lineage_path)
        except Exception as exc:
            self._publish(run, state="ERROR", detail=f"lineage write failed: {exc}")
            with self._lock:
                run.finished_at = self._now().astimezone(timezone.utc).isoformat()
            run.finished.set()
            raise TrainPreflightError(run.detail) from exc

        thread = threading.Thread(
            target=self._run_process,
            args=(run,),
            name=f"lerobot-train-{run_id[-16:]}",
            daemon=True,
        )
        run.thread = thread
        thread.start()
        return run_id

    def _run_process(self, run: _TrainRun) -> None:
        process = None
        try:
            if run.stop_requested.is_set():
                self._publish(
                    run,
                    state="ERROR",
                    detail="training stopped before subprocess start",
                )
                return
            log_path = run.run_path / "train.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = self._popen_factory(
                    run.command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                with self._lock:
                    run.process = process
                    stopping = run.stop_requested.is_set()
                if stopping:
                    self._begin_termination(run, process)
                else:
                    self._publish(run, state="RUNNING", detail="training subprocess running")

                if process.stdout is not None:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        progress = self.parse_progress(line)
                        if progress is not None:
                            self._publish(run, step=progress[0], loss=progress[1])
                returncode = process.wait()
                with self._lock:
                    run.returncode = returncode

            if run.stop_requested.is_set():
                self._publish(
                    run,
                    state="ERROR",
                    detail="training stopped before completion; checkpoint not claimed",
                )
            elif returncode != 0:
                self._publish(
                    run,
                    state="ERROR",
                    detail=f"training subprocess exited with code {returncode}",
                )
            elif not self._checkpoint_exists(run):
                self._publish(
                    run,
                    state="ERROR",
                    detail="training exited successfully but no checkpoint was saved",
                )
            else:
                self._publish(run, state="DONE", detail="training checkpoint saved")
        except Exception as exc:
            with self._lock:
                is_terminal = run.state in TERMINAL_STATES
            if not is_terminal:
                self._publish(run, state="ERROR", detail=f"training failed: {exc}")
        finally:
            with self._lock:
                run.finished_at = self._now().astimezone(timezone.utc).isoformat()
            run.finished.set()

    @staticmethod
    def _checkpoint_exists(run: _TrainRun) -> bool:
        checkpoint_root = run.output_path / "checkpoints"
        try:
            return checkpoint_root.is_dir() and any(
                path.is_file() for path in checkpoint_root.rglob("*")
            )
        except OSError:
            return False

    def _begin_termination(self, run: _TrainRun, process: Any) -> None:
        with self._lock:
            if run.termination_started:
                return
            run.termination_started = True
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass

        def kill_after_grace() -> None:
            if run.finished.wait(self._stop_grace_s):
                return
            try:
                if process.poll() is None:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass

        threading.Thread(
            target=kill_after_grace,
            name=f"lerobot-stop-{run.run_id[-16:]}",
            daemon=True,
        ).start()

    def stop(self, run_id: str) -> bool:
        """Request SIGTERM and escalate to SIGKILL after the configured grace."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state not in ACTIVE_STATES or run.stop_requested.is_set():
                return False
            run.stop_requested.set()
            process = run.process
        self._publish(run, state="STOPPING", detail="termination requested")
        if process is not None:
            self._begin_termination(run, process)
        return True

    def events_after(self, sequence: int) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            events = [dict(event) for number, event in self._events if number > sequence]
            return self._event_sequence, events

    def wait(self, run_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            run = self._runs[run_id]
        return run.finished.wait(timeout)
