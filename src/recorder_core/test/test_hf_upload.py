import json
from pathlib import Path
import threading

import pytest

from recorder_core.hf_upload import (
    HfUploadManager,
    UploadPreflightError,
    preflight_upload,
)


def make_dataset(tmp_path: Path, status: str = "PASS", name: str = "dataset") -> Path:
    dataset = tmp_path / name
    dataset.mkdir()
    (dataset / "validation.json").write_text(
        json.dumps({"status": status}), encoding="utf-8"
    )
    (dataset / "data.bin").write_bytes(b"dataset")
    return dataset


class FakeApi:
    def __init__(self):
        self.calls = []

    def create_repo(self, **kwargs):
        self.calls.append(("create_repo", kwargs))

    def upload_folder(self, **kwargs):
        self.calls.append(("upload_folder", kwargs))


def test_upload_success_transitions_and_updates_session(tmp_path):
    dataset = make_dataset(tmp_path)
    session_path = tmp_path / "sessions" / "one" / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({"session_id": "one"}), encoding="utf-8")
    api = FakeApi()
    manager = HfUploadManager(
        api_factory=lambda: api,
        now=lambda: "2026-07-14T12:00:00+00:00",
    )

    job_id = manager.start_upload(
        dataset_id="dataset",
        dataset_path=dataset,
        repo_id="owner/repo",
        token="secret-token",
        private=True,
        session_path=session_path,
    )

    assert manager.wait(job_id, 1.0)
    assert manager.get_status(job_id) == {
        "state": "done",
        "detail": "uploaded to owner/repo",
    }
    assert [name for name, _ in api.calls] == ["create_repo", "upload_folder"]
    assert api.calls[0][1] == {
        "repo_id": "owner/repo",
        "token": "secret-token",
        "private": True,
        "repo_type": "dataset",
        "exist_ok": True,
    }
    assert api.calls[1][1]["folder_path"] == dataset
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert session["hf_repo_id"] == "owner/repo"
    assert session["hf_uploaded_at"] == "2026-07-14T12:00:00+00:00"
    sequence, events = manager.events_after(0)
    assert sequence == 3
    assert [event["state"] for event in events] == [
        "preparing",
        "uploading",
        "done",
    ]
    assert all(event["type"] == "upload" for event in events)
    assert "secret-token" not in json.dumps(events)


def test_upload_cancel_stops_before_folder_upload(tmp_path):
    dataset = make_dataset(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingApi(FakeApi):
        def create_repo(self, **kwargs):
            super().create_repo(**kwargs)
            entered.set()
            assert release.wait(1.0)

    api = BlockingApi()
    manager = HfUploadManager(api_factory=lambda: api)
    job_id = manager.start_upload(
        dataset_id="dataset",
        dataset_path=dataset,
        repo_id="owner/repo",
        token="token",
        private=False,
    )
    assert entered.wait(1.0)

    assert manager.cancel(job_id)
    assert manager.get_status(job_id)["state"] == "cancelled"
    release.set()
    assert manager.wait(job_id, 1.0)
    assert [name for name, _ in api.calls] == ["create_repo"]
    assert not manager.cancel(job_id)


def test_upload_failure_is_terminal_and_redacts_token(tmp_path):
    dataset = make_dataset(tmp_path)
    token = "sensitive-token"

    class FailingApi(FakeApi):
        def upload_folder(self, **kwargs):
            raise RuntimeError(f"remote rejected {token}")

    manager = HfUploadManager(api_factory=FailingApi)
    job_id = manager.start_upload(
        dataset_id="dataset",
        dataset_path=dataset,
        repo_id="owner/repo",
        token=token,
        private=False,
    )

    assert manager.wait(job_id, 1.0)
    status = manager.get_status(job_id)
    assert status["state"] == "error"
    assert "[redacted]" in status["detail"]
    assert token not in status["detail"]


def test_preflight_rejects_failed_missing_and_partial_datasets(tmp_path):
    failed = make_dataset(tmp_path, status="FAIL", name="failed")
    with pytest.raises(UploadPreflightError, match="not PASS"):
        preflight_upload(failed)

    missing_validation = tmp_path / "missing-validation"
    missing_validation.mkdir()
    with pytest.raises(UploadPreflightError, match="missing"):
        preflight_upload(missing_validation)

    partial = make_dataset(tmp_path, name="staging.partial")
    with pytest.raises(UploadPreflightError, match="partial"):
        preflight_upload(partial)


def test_second_active_upload_for_dataset_is_rejected(tmp_path):
    dataset = make_dataset(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingApi(FakeApi):
        def create_repo(self, **kwargs):
            entered.set()
            assert release.wait(1.0)

    manager = HfUploadManager(api_factory=BlockingApi)
    manager.start_upload(
        dataset_id="dataset",
        dataset_path=dataset,
        repo_id="owner/one",
        token="token",
        private=False,
    )
    assert entered.wait(1.0)

    with pytest.raises(UploadPreflightError, match="already in progress"):
        manager.start_upload(
            dataset_id="dataset",
            dataset_path=dataset,
            repo_id="owner/two",
            token="token",
            private=False,
        )
    release.set()
