from pathlib import Path

import numpy as np
import pytest

from recorder_core.gate import check_frame
from recorder_core.manifest import load_manifest


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def valid_frame():
    manifest = load_manifest(MANIFEST_PATH)
    source_ages_ms = {
        key: 10.0 for key in manifest.recording.required_sources
    }
    vectors = {
        interface.key: np.zeros(interface.shape, dtype=interface.dtype)
        for interface in (*manifest.interfaces.state, *manifest.interfaces.action)
    }
    cameras = {camera.key: object() for camera in manifest.interfaces.cameras}
    return manifest, source_ages_ms, vectors, cameras


def test_gate_accepts_fresh_complete_frame():
    manifest, ages, vectors, cameras = valid_frame()

    assert check_frame(
        manifest, source_ages_ms=ages, vectors=vectors, cameras=cameras
    ) == (None, None)


def test_gate_reports_stale_source():
    manifest, ages, vectors, cameras = valid_frame()
    action = next(
        interface
        for interface in manifest.interfaces.action
        if interface.key == "action"
    )
    ages["action"] = action.freshness_ms + 1.0

    source, reason = check_frame(
        manifest, source_ages_ms=ages, vectors=vectors, cameras=cameras
    )

    assert source == "action"
    assert "stale" in reason


def test_gate_reports_missing_required_camera():
    manifest, ages, vectors, cameras = valid_frame()
    del cameras["top"]

    assert check_frame(
        manifest, source_ages_ms=ages, vectors=vectors, cameras=cameras
    ) == ("top", "required camera missing")


@pytest.mark.parametrize(
    ("invalid_vector", "reason_fragment"),
    [
        (np.zeros((11,), dtype=np.float32), "shape mismatch"),
        (np.zeros((12,), dtype=np.float64), "dtype mismatch"),
        (np.full((12,), np.nan, dtype=np.float32), "non-finite"),
    ],
)
def test_gate_checks_vector_schema(invalid_vector, reason_fragment):
    manifest, ages, vectors, cameras = valid_frame()
    vectors["action"] = invalid_vector

    source, reason = check_frame(
        manifest, source_ages_ms=ages, vectors=vectors, cameras=cameras
    )

    assert source == "action"
    assert reason_fragment in reason
