"""Threaded Hugging Face dataset uploads with in-memory credentials."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
import uuid


class UploadPreflightError(RuntimeError):
    """Raised before a job starts when a dataset is unsafe to upload."""


def preflight_upload(dataset_path: str | Path) -> None:
    """Require a promoted directory with a passing validation report."""
    path = Path(dataset_path)
    if path.name.endswith(".partial"):
        raise UploadPreflightError("partial datasets cannot be uploaded")
    if not path.is_dir():
        raise UploadPreflightError(f"dataset path does not exist: {path}")
    validation_path = path / "validation.json"
    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UploadPreflightError("validation.json is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadPreflightError("validation.json is unreadable") from exc
    if not isinstance(validation, dict) or validation.get("status") != "PASS":
        raise UploadPreflightError("dataset validation status is not PASS")


def _default_api_factory() -> Any:
    from huggingface_hub import HfApi

    return HfApi()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _UploadJob:
    dataset_id: str
    state: str = "preparing"
    detail: str = "preflight passed"
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class HfUploadManager:
    """Own upload jobs and expose thread-safe stage events."""

    def __init__(
        self,
        *,
        api_factory: Callable[[], Any] = _default_api_factory,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self._api_factory = api_factory
        self._event_callback = event_callback
        self._now = now
        self._lock = threading.Lock()
        self._jobs: dict[str, _UploadJob] = {}
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._event_sequence = 0

    def _publish(
        self, job_id: str, job: _UploadJob, state: str, detail: str
    ) -> None:
        with self._lock:
            job.state = state
            job.detail = detail
            self._event_sequence += 1
            event = {
                "type": "upload",
                "job_id": job_id,
                "dataset_id": job.dataset_id,
                "state": state,
                "detail": detail,
            }
            self._events.append((self._event_sequence, event))
        if self._event_callback is not None:
            try:
                self._event_callback(dict(event))
            except Exception:
                pass

    @staticmethod
    def _safe_error(exc: Exception, token: str) -> str:
        detail = str(exc) or exc.__class__.__name__
        if token:
            detail = detail.replace(token, "[redacted]")
        return detail

    def start_upload(
        self,
        *,
        dataset_id: str,
        dataset_path: str | Path,
        repo_id: str,
        token: str,
        private: bool,
        session_path: str | Path | None = None,
    ) -> str:
        """Validate synchronously, then start one upload per dataset."""
        with self._lock:
            if any(
                job.dataset_id == dataset_id and not job.finished.is_set()
                for job in self._jobs.values()
            ):
                raise UploadPreflightError(
                    f"upload already in progress for dataset: {dataset_id}"
                )
        preflight_upload(dataset_path)

        job_id = uuid.uuid4().hex
        job = _UploadJob(dataset_id=dataset_id)
        with self._lock:
            if any(
                current.dataset_id == dataset_id and not current.finished.is_set()
                for current in self._jobs.values()
            ):
                raise UploadPreflightError(
                    f"upload already in progress for dataset: {dataset_id}"
                )
            self._jobs[job_id] = job
        self._publish(job_id, job, "preparing", "preflight passed")
        thread = threading.Thread(
            target=self._run_upload,
            kwargs={
                "job_id": job_id,
                "job": job,
                "dataset_path": Path(dataset_path),
                "repo_id": repo_id,
                "token": token,
                "private": private,
                "session_path": None
                if session_path is None
                else Path(session_path),
            },
            name=f"hf-upload-{job_id[:8]}",
            daemon=True,
        )
        job.thread = thread
        thread.start()
        return job_id

    def _run_upload(
        self,
        *,
        job_id: str,
        job: _UploadJob,
        dataset_path: Path,
        repo_id: str,
        token: str,
        private: bool,
        session_path: Path | None,
    ) -> None:
        try:
            if job.cancel_requested.is_set():
                return
            api = self._api_factory()
            api.create_repo(
                repo_id=repo_id,
                token=token,
                private=private,
                repo_type="dataset",
                exist_ok=True,
            )
            if job.cancel_requested.is_set():
                return
            self._publish(job_id, job, "uploading", "uploading dataset folder")
            api.upload_folder(
                repo_id=repo_id,
                folder_path=dataset_path,
                token=token,
                repo_type="dataset",
            )
            if job.cancel_requested.is_set():
                return
            if session_path is not None:
                self._record_success(session_path, repo_id)
            if job.cancel_requested.is_set():
                return
            self._publish(job_id, job, "done", f"uploaded to {repo_id}")
        except Exception as exc:
            if not job.cancel_requested.is_set():
                self._publish(job_id, job, "error", self._safe_error(exc, token))
        finally:
            job.finished.set()
            token = ""

    def _record_success(self, session_path: Path, repo_id: str) -> None:
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"failed to read session.json: {exc}") from exc
        if not isinstance(session, dict):
            raise RuntimeError("failed to read session.json: expected an object")
        session["hf_repo_id"] = repo_id
        session["hf_uploaded_at"] = self._now()
        temporary = session_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(session, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(session_path)
        except OSError as exc:
            raise RuntimeError(f"failed to update session.json: {exc}") from exc

    def get_status(self, job_id: str) -> dict[str, str]:
        with self._lock:
            job = self._jobs[job_id]
            return {"state": job.state, "detail": job.detail}

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.finished.is_set() or job.cancel_requested.is_set():
                return False
            job.cancel_requested.set()
        self._publish(job_id, job, "cancelled", "cancellation requested")
        return True

    def events_after(self, sequence: int) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            events = [dict(event) for number, event in self._events if number > sequence]
            return self._event_sequence, events

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            job = self._jobs[job_id]
        return job.finished.wait(timeout)
