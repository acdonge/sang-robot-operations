"""LeRobot v3 writer boundary.

This is the only recorder_core module that imports LeRobot.  The wrapper uses
the public LeRobotDataset create/add_frame/save_episode/finalize lifecycle.
"""

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from recorder_core.manifest import Manifest


PINNED_LEROBOT_SHA = "30da8e687a6dfc617fcd94afc367ac7071c376ce"


class WriterError(RuntimeError):
    """Raised when a frame cannot be written under the manifest contract."""


def _dataset_class():
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as exc:
        raise WriterError(
            "LeRobot is unavailable; run recorder_core in the pinned dataset container"
        ) from exc
    return LeRobotDataset


def lerobot_revision() -> str:
    """Return the exact configured LeRobot revision recorded in session lineage."""
    return os.environ.get("LEROBOT_SHA", PINNED_LEROBOT_SHA)


def features_from_manifest(manifest: Manifest) -> dict[str, dict[str, Any]]:
    """Build the public LeRobot feature schema from a validated manifest."""
    features: dict[str, dict[str, Any]] = {}
    for camera in manifest.interfaces.cameras:
        features[f"observation.images.{camera.key}"] = {
            "dtype": "video",
            "shape": (camera.height, camera.width, 3),
            "names": ["height", "width", "channel"],
        }
    for interface in (*manifest.interfaces.state, *manifest.interfaces.action):
        features[interface.key] = {
            "dtype": interface.dtype,
            "shape": interface.shape,
            "names": list(interface.feature_names),
        }
    return features


def dataset_repo_id(dataset_root: str | Path) -> str:
    """Derive the local-only repo id used to reopen a dataset from its root."""
    name = Path(dataset_root).name
    if name.endswith(".partial"):
        name = name[: -len(".partial")]
    return name


def open_dataset(dataset_root: str | Path):
    """Reopen a finalized local dataset through the public LeRobot API."""
    root = Path(dataset_root)
    return _dataset_class()(repo_id=dataset_repo_id(root), root=root)


class LeRobotV3Writer:
    """One-episode LeRobot v3 dataset writer rooted in session staging."""

    def __init__(self, manifest: Manifest, session_id: str, dataset_id: str) -> None:
        self.manifest = manifest
        self.session_id = session_id
        self.dataset_id = dataset_id
        self.staging_path = (
            Path(manifest.recording.staging_root) / f"{session_id}.partial"
        )
        self.staging_path.parent.mkdir(parents=True, exist_ok=True)
        if self.staging_path.exists():
            raise WriterError(f"staging path already exists: {self.staging_path}")

        try:
            self._dataset = _dataset_class().create(
                repo_id=session_id,
                root=self.staging_path,
                fps=manifest.recording.fps,
                features=features_from_manifest(manifest),
                robot_type=manifest.robot.model,
                use_videos=True,
            )
        except Exception as exc:
            raise WriterError(f"failed to create LeRobot dataset: {exc}") from exc
        self.frame_count = 0
        self._episode_saved = False
        self._finalized = False

    @staticmethod
    def _decode_jpeg(value: Any, camera_key: str) -> np.ndarray:
        payload = getattr(value, "data", value)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            # rclpy CompressedImage.data는 array.array('B')로 도착한다 — buffer 프로토콜이면 수용
            try:
                payload = bytes(payload)
            except TypeError:
                raise WriterError(f"camera {camera_key!r} must contain JPEG bytes") from None
        encoded = np.frombuffer(payload, dtype=np.uint8)
        bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if bgr is None:
            raise WriterError(f"camera {camera_key!r} JPEG decode failed")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def add_frame(self, frame: Mapping[str, Any], *, task: str) -> None:
        """Decode manifest cameras and append one task-labelled frame."""
        if self._episode_saved or self._finalized:
            raise WriterError("cannot add a frame after episode save/finalize")
        dataset_frame: dict[str, Any] = {"task": task}

        for interface in (*self.manifest.interfaces.state, *self.manifest.interfaces.action):
            if interface.key not in frame:
                raise WriterError(f"frame is missing {interface.key!r}")
            vector = np.asarray(frame[interface.key])
            if vector.shape != interface.shape or vector.dtype != np.dtype(interface.dtype):
                raise WriterError(
                    f"{interface.key} schema mismatch: got {vector.shape}/{vector.dtype}, "
                    f"expected {interface.shape}/{interface.dtype}"
                )
            dataset_frame[interface.key] = vector

        for camera in self.manifest.interfaces.cameras:
            if camera.key not in frame:
                raise WriterError(f"frame is missing camera {camera.key!r}")
            rgb = self._decode_jpeg(frame[camera.key], camera.key)
            expected_shape = (camera.height, camera.width, 3)
            if rgb.shape != expected_shape:
                raise WriterError(
                    f"camera {camera.key!r} shape mismatch: got {rgb.shape}, "
                    f"expected {expected_shape}"
                )
            dataset_frame[f"observation.images.{camera.key}"] = rgb

        try:
            self._dataset.add_frame(dataset_frame)
        except Exception as exc:
            raise WriterError(f"LeRobot add_frame failed: {exc}") from exc
        self.frame_count += 1

    def save_episode(self) -> None:
        if self._episode_saved:
            raise WriterError("episode was already saved")
        if self.frame_count <= 0:
            raise WriterError("cannot save an empty episode")
        try:
            self._dataset.save_episode()
        except Exception as exc:
            raise WriterError(f"LeRobot save_episode failed: {exc}") from exc
        self._episode_saved = True

    def finalize(self) -> None:
        if not self._episode_saved:
            raise WriterError("save_episode must succeed before finalize")
        if self._finalized:
            return
        try:
            self._dataset.finalize()
        except Exception as exc:
            raise WriterError(f"LeRobot finalize failed: {exc}") from exc
        self._finalized = True
