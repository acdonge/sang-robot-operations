"""Pure-Python frame freshness and schema gate."""

import math
from typing import Any, Mapping

import numpy as np

from recorder_core.manifest import Manifest


GateFailure = tuple[str | None, str | None]


def check_frame(
    manifest: Manifest,
    *,
    source_ages_ms: Mapping[str, float],
    vectors: Mapping[str, np.ndarray],
    cameras: Mapping[str, Any],
) -> GateFailure:
    """Return ``(None, None)`` on success or ``(source, reason)`` on failure."""
    required = set(manifest.recording.required_sources)
    interfaces = {
        interface.key: interface
        for interface in (
            *manifest.interfaces.state,
            *manifest.interfaces.action,
            *manifest.interfaces.cameras,
        )
    }

    for camera in manifest.interfaces.cameras:
        if camera.key in required and cameras.get(camera.key) is None:
            return camera.key, "required camera missing"

    for source_key in manifest.recording.required_sources:
        age = source_ages_ms.get(source_key)
        if age is None or not isinstance(age, (int, float)) or not math.isfinite(age):
            return source_key, "no received sample"
        freshness_ms = interfaces[source_key].freshness_ms
        if age > freshness_ms:
            return (
                source_key,
                f"stale: age_ms={age:.1f} exceeds freshness_ms={freshness_ms}",
            )

    for interface in (*manifest.interfaces.state, *manifest.interfaces.action):
        if interface.key not in required:
            continue
        vector = vectors.get(interface.key)
        if vector is None:
            return interface.key, "feature vector missing"
        if not isinstance(vector, np.ndarray):
            return interface.key, "feature vector must be a numpy.ndarray"
        if vector.shape != interface.shape:
            return (
                interface.key,
                f"shape mismatch: got {vector.shape}, expected {interface.shape}",
            )
        expected_dtype = np.dtype(interface.dtype)
        if vector.dtype != expected_dtype:
            return (
                interface.key,
                f"dtype mismatch: got {vector.dtype}, expected {expected_dtype}",
            )
        if not np.all(np.isfinite(vector)):
            return interface.key, "feature vector contains non-finite values"

    return None, None
