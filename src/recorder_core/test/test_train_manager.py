import json
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pytest

from recorder_core.train_manager import (
    TrainConflictError,
    TrainManager,
    TrainPreflightError,
    meta_digest,
    preflight_train,
)


FIXED_NOW = datetime(2026, 7, 14, 12, 34, 56, 123456, tzinfo=timezone.utc)


def make_dataset(tmp_path: Path, *, status: str = "PASS") -> Path:
    dataset = tmp_path / "store" / "datasets" / "raw" / "dataset-1"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text(
        json.dumps({"total_episodes": 1, "total_frames": 20, "fps": 10}),
        encoding="utf-8",
    )
    (dataset / "meta" / "stats.json").write_text(
        json.dumps({"action": {"mean": [0.0]}}), encoding="utf-8"
    )
    (dataset / "validation.json").write_text(
        json.dumps({"status": status}), encoding="utf-8"
    )
    return dataset


def command_value(command: list[str], name: str) -> str:
    prefix = f"--{name}="
    return next(value[len(prefix) :] for value in command if value.startswith(prefix))


class FinishedProcess:
    def __init__(self, lines=(), returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class BlockingProcess:
    def __init__(self, *, exit_on_term: bool):
        self.stdout = iter(())
        self.exit_on_term = exit_on_term
        self.released = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.returncode = None

    def wait(self):
        assert self.released.wait(1.0)
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.exit_on_term:
            self.returncode = -15
            self.released.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.released.set()


def wait_for_state(manager: TrainManager, run_id: str, state: str) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if manager.get_run(run_id)["state"] == state:
            return
        time.sleep(0.005)
    raise AssertionError(f"run did not reach {state}: {manager.get_run(run_id)}")


def test_command_lineage_progress_and_checkpoint_gated_done(tmp_path):
    dataset = make_dataset(tmp_path)
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command_value(command, "output_dir"))
        checkpoint = output / "checkpoints" / "000020" / "pretrained_model"
        checkpoint.mkdir(parents=True)
        (checkpoint / "model.safetensors").write_bytes(b"weights")
        return FinishedProcess(
            [
                "INFO step:1 smpl:1 ep:0.1 epch:0.05 loss:1.250 grdn:0.2\n",
                "INFO step:20 smpl:20 ep:1 epch:1.00 loss:2.5e-2 grdn:0.1\n",
            ]
        )

    manager = TrainManager(
        tmp_path / "store",
        popen_factory=popen,
        now=lambda: FIXED_NOW,
        revision=lambda: "pinned-sha",
    )
    run_id = manager.start_train(
        dataset_id="dataset-1",
        dataset_path=dataset,
        policy_type="act",
        steps=20,
    )

    assert manager.wait(run_id, 1.0)
    status = manager.get_run(run_id)
    assert status["state"] == "DONE"
    assert status["step"] == 20
    assert status["loss"] == 0.025
    assert status["returncode"] == 0
    assert run_id == "dataset-1__act__20260714T123456123456Z"

    command, options = calls[0]
    assert command[0] == "lerobot-train"
    assert command_value(command, "dataset.root") == str(dataset.resolve())
    assert command_value(command, "dataset.repo_id") == "dataset-1"
    assert command_value(command, "policy.type") == "act"
    assert command_value(command, "policy.device") == "cpu"
    assert command_value(command, "policy.dim_model") == "64"
    assert command_value(command, "policy.pretrained_backbone_weights") == "null"
    assert command_value(command, "policy.push_to_hub") == "false"
    assert command_value(command, "steps") == "20"
    assert command_value(command, "log_freq") == "1"
    assert command_value(command, "save_freq") == "20"
    assert command_value(command, "output_dir").endswith(f"{run_id}/output")
    assert options["stderr"] is not None
    assert options["text"] is True

    lineage_path = Path(status["path"]) / "lineage.json"
    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    assert lineage["dataset"] == {
        "dataset_id": "dataset-1",
        "path": str(dataset.resolve()),
        "meta_digest": {"algorithm": "sha256", "value": meta_digest(dataset)},
    }
    assert lineage["lerobot_revision"] == "pinned-sha"
    assert lineage["command"] == command
    assert lineage["resume_supported"] is False
    assert not (Path(status["path"]) / "output" / "lineage.json").exists()

    _, events = manager.events_after(0)
    assert [event["state"] for event in events] == [
        "PREPARING",
        "RUNNING",
        "RUNNING",
        "RUNNING",
        "DONE",
    ]
    assert all(set(event) == {"type", "run_id", "state", "step", "loss"} for event in events)


def test_zero_exit_without_checkpoint_is_error_not_false_success(tmp_path):
    dataset = make_dataset(tmp_path)
    manager = TrainManager(
        tmp_path / "store",
        popen_factory=lambda *args, **kwargs: FinishedProcess(
            ["step:1 smpl:1 loss:0.500\n"], returncode=0
        ),
        now=lambda: FIXED_NOW,
    )

    run_id = manager.start_train(
        dataset_id="dataset-1",
        dataset_path=dataset,
        policy_type="act",
        steps=1,
    )

    assert manager.wait(run_id, 1.0)
    status = manager.get_run(run_id)
    assert status["state"] == "ERROR"
    assert "no checkpoint" in status["detail"]


def test_nonzero_or_spawn_failure_becomes_error_and_does_not_block_next_run(tmp_path):
    dataset = make_dataset(tmp_path)
    attempts = 0

    def popen(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError("lerobot-train")
        return FinishedProcess(returncode=2)

    times = iter(
        [
            FIXED_NOW,
            FIXED_NOW,
            FIXED_NOW.replace(microsecond=123457),
            FIXED_NOW.replace(microsecond=123457),
        ]
    )
    manager = TrainManager(tmp_path / "store", popen_factory=popen, now=lambda: next(times))
    first = manager.start_train(
        dataset_id="dataset-1", dataset_path=dataset, policy_type="act", steps=1
    )
    assert manager.wait(first, 1.0)
    assert manager.get_run(first)["state"] == "ERROR"
    assert "lerobot-train" in manager.get_run(first)["detail"]

    second = manager.start_train(
        dataset_id="dataset-1", dataset_path=dataset, policy_type="act", steps=1
    )
    assert manager.wait(second, 1.0)
    assert manager.get_run(second)["state"] == "ERROR"
    assert manager.get_run(second)["returncode"] == 2


@pytest.mark.parametrize("exit_on_term, expected_kills", [(True, 0), (False, 1)])
def test_stop_transitions_and_escalates_after_grace(
    tmp_path, exit_on_term, expected_kills
):
    dataset = make_dataset(tmp_path)
    process = BlockingProcess(exit_on_term=exit_on_term)
    manager = TrainManager(
        tmp_path / "store",
        popen_factory=lambda *args, **kwargs: process,
        now=lambda: FIXED_NOW,
        stop_grace_s=0.01,
    )
    run_id = manager.start_train(
        dataset_id="dataset-1", dataset_path=dataset, policy_type="act", steps=20
    )
    wait_for_state(manager, run_id, "RUNNING")

    assert manager.stop(run_id)
    assert manager.wait(run_id, 1.0)
    assert manager.get_run(run_id)["state"] == "ERROR"
    assert process.terminate_calls == 1
    assert process.kill_calls == expected_kills
    assert not manager.stop(run_id)
    assert [event["state"] for event in manager.events_after(0)[1]][-2:] == [
        "STOPPING",
        "ERROR",
    ]


def test_only_one_run_may_be_active_and_unknown_stop_raises(tmp_path):
    dataset = make_dataset(tmp_path)
    process = BlockingProcess(exit_on_term=True)
    manager = TrainManager(
        tmp_path / "store",
        popen_factory=lambda *args, **kwargs: process,
        now=lambda: FIXED_NOW,
    )
    run_id = manager.start_train(
        dataset_id="dataset-1", dataset_path=dataset, policy_type="act", steps=20
    )
    wait_for_state(manager, run_id, "RUNNING")

    with pytest.raises(TrainConflictError, match="already active"):
        manager.start_train(
            dataset_id="dataset-1",
            dataset_path=dataset,
            policy_type="act",
            steps=20,
        )
    with pytest.raises(KeyError):
        manager.stop("missing")

    assert manager.stop(run_id)
    assert manager.wait(run_id, 1.0)


def test_progress_parser_and_preflight_boundaries(tmp_path):
    assert TrainManager.parse_progress(
        "2026 INFO step:1,234 smpl:1K epch:1.0 loss:-1.2e-03 grdn:0.1"
    ) == (1234, -0.0012)
    assert TrainManager.parse_progress("ordinary log without metrics") is None
    assert TrainManager.parse_progress("step:one loss:nan") is None

    dataset = make_dataset(tmp_path, status="FAIL")
    with pytest.raises(TrainPreflightError, match="not PASS"):
        preflight_train(dataset)
    (dataset / "validation.json").write_text(
        json.dumps({"status": "PASS"}), encoding="utf-8"
    )
    (dataset / "meta" / "info.json").unlink()
    with pytest.raises(TrainPreflightError, match="info.json"):
        preflight_train(dataset)
