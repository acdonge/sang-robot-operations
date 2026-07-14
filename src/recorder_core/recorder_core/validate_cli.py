"""Independent-process validator for one finalized LeRobot v3 dataset."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from recorder_core.manifest import Manifest, load_manifest
from recorder_core.writer import open_dataset


def _as_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return value.numpy()
    return np.asarray(value)


def _ordered_names(feature: dict[str, Any]) -> tuple[str, ...]:
    names = feature.get("names", ())
    if isinstance(names, dict):
        flattened = []
        for group in names.values():
            flattened.extend(group)
        return tuple(flattened)
    return tuple(names)


def _validate_metadata(dataset: Any, manifest: Manifest, errors: list[str]) -> None:
    if dataset.fps != manifest.recording.fps:
        errors.append(f"fps mismatch: got {dataset.fps}, expected {manifest.recording.fps}")
    if dataset.num_frames <= 0:
        errors.append("dataset has no frames")
    if dataset.num_episodes <= 0:
        errors.append("dataset has no episodes")

    expected_camera_keys = {
        f"observation.images.{camera.key}" for camera in manifest.interfaces.cameras
    }
    actual_camera_keys = {
        key
        for key, feature in dataset.features.items()
        if feature.get("dtype") == "video"
    }
    if actual_camera_keys != expected_camera_keys:
        errors.append(
            "camera keys mismatch: got %s, expected %s"
            % (sorted(actual_camera_keys), sorted(expected_camera_keys))
        )

    for camera in manifest.interfaces.cameras:
        key = f"observation.images.{camera.key}"
        feature = dataset.features.get(key)
        if feature is None:
            continue
        expected_shape = (camera.height, camera.width, 3)
        if tuple(feature.get("shape", ())) != expected_shape:
            errors.append(
                f"{key} metadata shape mismatch: got {feature.get('shape')}, "
                f"expected {expected_shape}"
            )

    for interface in (*manifest.interfaces.state, *manifest.interfaces.action):
        feature = dataset.features.get(interface.key)
        if feature is None:
            errors.append(f"missing feature metadata: {interface.key}")
            continue
        if tuple(feature.get("shape", ())) != interface.shape:
            errors.append(
                f"{interface.key} metadata shape mismatch: got {feature.get('shape')}, "
                f"expected {interface.shape}"
            )
        if np.dtype(feature.get("dtype")) != np.dtype(interface.dtype):
            errors.append(
                f"{interface.key} metadata dtype mismatch: got {feature.get('dtype')}, "
                f"expected {interface.dtype}"
            )
        names = _ordered_names(feature)
        if names != interface.feature_names:
            errors.append(
                f"{interface.key} names order mismatch: got {names}, "
                f"expected {interface.feature_names}"
            )


def _validate_frames(dataset: Any, manifest: Manifest, errors: list[str]) -> dict[str, int]:
    decoded = {
        f"observation.images.{camera.key}": 0
        for camera in manifest.interfaces.cameras
    }
    for index in range(dataset.num_frames):
        try:
            sample = dataset[index]
        except Exception as exc:
            errors.append(f"frame {index} reopen/decode failed: {exc}")
            break

        for interface in (*manifest.interfaces.state, *manifest.interfaces.action):
            if interface.key not in sample:
                errors.append(f"frame {index} missing {interface.key}")
                continue
            vector = _as_numpy(sample[interface.key])
            if vector.shape != interface.shape:
                errors.append(
                    f"frame {index} {interface.key} shape mismatch: "
                    f"got {vector.shape}, expected {interface.shape}"
                )
            if vector.dtype != np.dtype(interface.dtype):
                errors.append(
                    f"frame {index} {interface.key} dtype mismatch: "
                    f"got {vector.dtype}, expected {interface.dtype}"
                )
        for camera in manifest.interfaces.cameras:
            key = f"observation.images.{camera.key}"
            if key not in sample:
                errors.append(f"frame {index} missing decoded video feature {key}")
                continue
            image = _as_numpy(sample[key])
            valid_shapes = {
                (camera.height, camera.width, 3),
                (3, camera.height, camera.width),
            }
            if image.shape not in valid_shapes:
                errors.append(
                    f"frame {index} {key} decoded shape mismatch: got {image.shape}"
                )
            decoded[key] += 1

        for key, value in sample.items():
            try:
                numeric = _as_numpy(value)
            except (TypeError, ValueError):
                continue
            if np.issubdtype(numeric.dtype, np.number) and not np.all(
                np.isfinite(numeric)
            ):
                errors.append(f"frame {index} {key} contains non-finite values")

    for key, count in decoded.items():
        if count != dataset.num_frames:
            errors.append(
                f"{key} decoded frame count mismatch: got {count}, "
                f"expected {dataset.num_frames}"
            )
    return decoded


def validate_dataset(dataset_root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(dataset_root)
    errors: list[str] = []
    decoded_counts: dict[str, int] = {}
    frame_count = 0
    episode_count = 0
    fps = None
    cache_variables = ("HF_HOME", "HF_DATASETS_CACHE", "HUGGINGFACE_HUB_CACHE")
    previous_cache = {name: os.environ.get(name) for name in cache_variables}
    with tempfile.TemporaryDirectory(prefix="validate_dataset_") as cache_directory:
        cache_root = Path(cache_directory)
        os.environ["HF_HOME"] = str(cache_root / "hf")
        os.environ["HF_DATASETS_CACHE"] = str(cache_root / "datasets")
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache_root / "hub")
        try:
            manifest = load_manifest(manifest_path)
            dataset = open_dataset(root)
            frame_count = int(dataset.num_frames)
            episode_count = int(dataset.num_episodes)
            fps = int(dataset.fps)
            _validate_metadata(dataset, manifest, errors)
            decoded_counts = _validate_frames(dataset, manifest, errors)
        except Exception as exc:
            errors.append(f"dataset reopen failed: {exc}")
        finally:
            for name, value in previous_cache.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    result = {
        "status": "PASS" if not errors else "FAIL",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root),
        "manifest": str(Path(manifest_path)),
        "frame_count": frame_count,
        "episode_count": episode_count,
        "fps": fps,
        "decoded_video_frames": decoded_counts,
        "errors": errors,
    }
    root.mkdir(parents=True, exist_ok=True)
    output_path = root / "validation.json"
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(output_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    result = validate_dataset(args.dataset_root, args.manifest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
