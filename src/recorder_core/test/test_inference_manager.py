from pathlib import Path
from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest
import torch

from recorder_core.inference_manager import (
    InferenceConflictError,
    InferenceManager,
    InferencePreflightError,
    PolicyRuntime,
    final_checkpoint,
    manifest_policy_features,
    preflight_policy_schema,
)
from recorder_core.manifest import load_manifest


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def policy_for_manifest(manifest, *, action_shape=(12,)):
    inputs, outputs = manifest_policy_features(manifest)
    outputs["action"] = action_shape
    config = SimpleNamespace(
        input_features={
            key: SimpleNamespace(shape=shape) for key, shape in inputs.items()
        },
        output_features={
            key: SimpleNamespace(shape=shape) for key, shape in outputs.items()
        },
    )

    class Policy:
        def __init__(self):
            self.config = config
            self.reset_calls = 0
            self.select_calls = 0

        def reset(self):
            self.reset_calls += 1

        def select_action(self, observation):
            self.select_calls += 1
            assert observation["observation.state"].shape == (12,)
            assert observation["observation.images.top"].shape == (3, 480, 640)
            return torch.zeros((1, 12), dtype=torch.float32)

    return Policy()


def make_checkpoint(tmp_path, run_id):
    checkpoint = (
        tmp_path
        / "store"
        / "runs"
        / run_id
        / "output"
        / "checkpoints"
        / "last"
        / "pretrained_model"
    )
    checkpoint.mkdir(parents=True)
    (checkpoint / "config.json").write_text("{}", encoding="utf-8")
    return checkpoint


def make_sample(manifest, *, stale=False):
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    encoded, jpeg = cv2.imencode(".jpg", image)
    assert encoded
    state = np.zeros((12,), dtype=np.float32)
    cameras = {camera.key: jpeg.tobytes() for camera in manifest.interfaces.cameras}
    source_ages = {
        interface.key: (2000.0 if stale else 0.0)
        for interface in manifest.interfaces.state
    }
    source_ages.update(
        {camera.key: (2000.0 if stale else 0.0) for camera in manifest.interfaces.cameras}
    )
    return {
        "observation.state": state,
        **cameras,
        "_source_ages_ms": source_ages,
        "_vectors": {"observation.state": state},
        "_cameras": cameras,
    }


class FakeNode:
    def __init__(self, manifest, sample):
        self.manifest = manifest
        self.sample = sample
        self.actions = []

    def get_inference_sample(self):
        return self.sample

    def publish_policy_action(self, action):
        self.actions.append(np.asarray(action).copy())


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_schema_preflight_rejects_mock_policy_shape_mismatch():
    manifest = load_manifest(MANIFEST_PATH)
    policy = policy_for_manifest(manifest, action_shape=(11,))

    with pytest.raises(InferencePreflightError, match="schema mismatch") as exc:
        preflight_policy_schema(policy, manifest)

    assert "output 'action' shape=(11,), expected=(12,)" in str(exc.value)


def test_final_checkpoint_prefers_last_then_highest_numbered(tmp_path):
    run_path = tmp_path / "run"
    older = run_path / "output" / "checkpoints" / "000010" / "pretrained_model"
    newer = run_path / "output" / "checkpoints" / "000020" / "pretrained_model"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "model.safetensors").write_bytes(b"old")
    (newer / "model.safetensors").write_bytes(b"new")

    assert final_checkpoint(run_path) == newer.resolve()

    preferred = run_path / "output" / "checkpoints" / "last" / "pretrained_model"
    preferred.mkdir(parents=True)
    (preferred / "config.json").write_text("{}", encoding="utf-8")
    assert final_checkpoint(run_path) == preferred.resolve()


def test_policy_listing_filters_done_and_checkpoint(tmp_path):
    done = "dataset__act__done"
    active = "dataset__act__active"
    make_checkpoint(tmp_path, done)
    make_checkpoint(tmp_path, active)
    runs = [
        {"run_id": done, "state": "DONE", "policy_type": "act"},
        {"run_id": active, "state": "RUNNING", "policy_type": "act"},
        {"run_id": "missing", "state": "DONE", "policy_type": "act"},
    ]
    manager = InferenceManager(
        tmp_path / "store", run_supplier=lambda: runs
    )

    assert [policy["run_id"] for policy in manager.list_policies()] == [done]


def test_start_stop_transitions_publish_and_are_idempotent(tmp_path):
    manifest = load_manifest(MANIFEST_PATH)
    run_id = "dataset__act__done"
    make_checkpoint(tmp_path, run_id)
    run = {"run_id": run_id, "state": "DONE", "policy_type": "act"}
    policy = policy_for_manifest(manifest)
    runtime = PolicyRuntime(policy, lambda value: value, lambda value: value, torch)
    node = FakeNode(manifest, make_sample(manifest))
    manager = InferenceManager(
        tmp_path / "store",
        run_supplier=lambda: [run],
        policy_loader=lambda path: runtime,
    )

    assert manager.status()["state"] == "STOPPED"
    started = manager.start(run_id, node)
    assert started["state"] == "RUNNING"
    assert manager.start(run_id, node)["state"] == "RUNNING"
    with pytest.raises(InferenceConflictError, match="already active"):
        manager.start("different", node)
    assert wait_for(lambda: len(node.actions) >= 1)
    assert node.actions[0].shape == (12,)

    assert manager.stop()["state"] == "STOPPED"
    action_count = len(node.actions)
    time.sleep(0.08)
    assert len(node.actions) == action_count
    assert manager.stop()["state"] == "STOPPED"


def test_start_rejects_schema_mismatch_and_latches_error(tmp_path):
    manifest = load_manifest(MANIFEST_PATH)
    run_id = "dataset__act__done"
    make_checkpoint(tmp_path, run_id)
    run = {"run_id": run_id, "state": "DONE", "policy_type": "act"}
    policy = policy_for_manifest(manifest, action_shape=(11,))
    runtime = PolicyRuntime(policy, lambda value: value, lambda value: value, torch)
    node = FakeNode(manifest, make_sample(manifest))
    manager = InferenceManager(
        tmp_path / "store",
        run_supplier=lambda: [run],
        policy_loader=lambda path: runtime,
    )

    with pytest.raises(InferencePreflightError, match="schema mismatch"):
        manager.start(run_id, node)

    assert manager.status()["state"] == "ERROR"
    assert node.actions == []
    assert manager.stop()["state"] == "STOPPED"


def test_stale_observation_enters_error_without_publishing(tmp_path):
    manifest = load_manifest(MANIFEST_PATH)
    run_id = "dataset__act__done"
    make_checkpoint(tmp_path, run_id)
    run = {"run_id": run_id, "state": "DONE", "policy_type": "act"}
    policy = policy_for_manifest(manifest)
    runtime = PolicyRuntime(policy, lambda value: value, lambda value: value, torch)
    node = FakeNode(manifest, make_sample(manifest, stale=True))
    manager = InferenceManager(
        tmp_path / "store",
        run_supplier=lambda: [run],
        policy_loader=lambda path: runtime,
    )

    manager.start(run_id, node)
    assert wait_for(lambda: manager.status()["state"] == "ERROR")
    status = manager.status()
    assert "observation gate failed" in status["detail"]
    assert node.actions == []
    assert manager.stop()["state"] == "STOPPED"
