from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from recorder_core.manifest import load_manifest
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
