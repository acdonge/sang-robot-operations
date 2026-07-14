from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from recorder_core.manifest import load_manifest
import recorder_core.writer as writer_module
from recorder_core.writer import LeRobotV3Writer, features_from_manifest


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def test_features_are_derived_from_manifest_in_contract_order():
    manifest = load_manifest(MANIFEST_PATH)

    features = features_from_manifest(manifest)

    assert features["observation.images.top"] == {
        "dtype": "video",
        "shape": (480, 640, 3),
        "names": ["height", "width", "channel"],
    }
    assert features["observation.state"]["dtype"] == "float32"
    assert features["observation.state"]["shape"] == (12,)
    assert features["observation.state"]["names"] == list(
        manifest.interfaces.state[0].feature_names
    )
    assert features["action"]["names"] == list(
        manifest.interfaces.action[0].feature_names
    )


def test_jpeg_decode_converts_bgr_to_rgb():
    bgr = np.zeros((32, 32, 3), dtype=np.uint8)
    bgr[:] = (10, 40, 220)
    encoded, jpeg = cv2.imencode(".jpg", bgr)
    assert encoded

    rgb = LeRobotV3Writer._decode_jpeg(
        SimpleNamespace(data=jpeg.tobytes()), "top"
    )

    assert rgb.shape == (32, 32, 3)
    assert rgb.dtype == np.uint8
    assert rgb[16, 16, 0] > 200
    assert rgb[16, 16, 2] < 30


class FakeDataset:
    created = None

    def __init__(self):
        self.calls = []

    @classmethod
    def create(cls, **kwargs):
        cls.created = cls()
        cls.created.create_kwargs = kwargs
        return cls.created

    def add_frame(self, frame):
        self.calls.append(("add_frame", frame))

    def save_episode(self):
        self.calls.append(("save_episode",))

    def clear_episode_buffer(self):
        self.calls.append(("clear_episode_buffer",))

    def finalize(self):
        self.calls.append(("finalize",))


def test_writer_keeps_dataset_open_across_save_retry_and_finish(tmp_path, monkeypatch):
    base = load_manifest(MANIFEST_PATH)
    manifest = replace(
        base,
        recording=replace(base.recording, staging_root=str(tmp_path / "staging")),
    )
    monkeypatch.setattr(writer_module, "_dataset_class", lambda: FakeDataset)
    writer = LeRobotV3Writer(manifest, "session-1", "dataset-1")

    writer.frame_count = 12
    writer.total_frame_count = 12
    writer.save_episode()
    assert writer.episode_count == 1
    assert writer.frame_count == 0

    writer.frame_count = 5
    writer.total_frame_count += 5
    stale_images = (
        writer.staging_path
        / "images"
        / "chunk-000"
        / "observation.images.top"
        / "episode_000001"
    )
    stale_images.mkdir(parents=True)
    (stale_images / "frame_000004.png").write_bytes(b"stale")
    writer.discard_episode()
    assert writer.episode_count == 1
    assert writer.frame_count == 0
    assert writer.total_frame_count == 12
    assert not stale_images.exists()

    writer.frame_count = 9
    writer.total_frame_count += 9
    writer.save_episode()
    writer.finalize()

    assert writer.episode_count == 2
    assert writer.total_frame_count == 21
    assert FakeDataset.created.calls == [
        ("save_episode",),
        ("clear_episode_buffer",),
        ("save_episode",),
        ("finalize",),
    ]


def test_writer_rejects_empty_save_and_unsaved_finalize(tmp_path, monkeypatch):
    base = load_manifest(MANIFEST_PATH)
    manifest = replace(
        base,
        recording=replace(base.recording, staging_root=str(tmp_path / "staging")),
    )
    monkeypatch.setattr(writer_module, "_dataset_class", lambda: FakeDataset)
    writer = LeRobotV3Writer(manifest, "session-2", "dataset-2")

    with pytest.raises(writer_module.WriterError, match="empty episode"):
        writer.save_episode()
    with pytest.raises(writer_module.WriterError, match="at least one episode"):
        writer.finalize()
