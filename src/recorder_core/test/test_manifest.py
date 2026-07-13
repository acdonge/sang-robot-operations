from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from recorder_core.manifest import (
    ManifestValidationError,
    load_manifest,
    parse_manifest,
)


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


@pytest.fixture
def manifest_data():
    with MANIFEST_PATH.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_example_manifest_loads():
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest.schema_version == "0.1"
    assert manifest.robot.robot_id == "bi_so101_01"
    assert len(manifest.interfaces.cameras) == 5
    assert manifest.state_feature_dimension == 12
    assert manifest.recording.fps == 30


def test_missing_required_field_fails(manifest_data):
    invalid = deepcopy(manifest_data)
    del invalid["robot"]["robot_id"]

    with pytest.raises(ManifestValidationError, match="robot.robot_id"):
        parse_manifest(invalid)


def test_zero_cameras_fails(manifest_data):
    invalid = deepcopy(manifest_data)
    invalid["interfaces"]["cameras"] = []

    with pytest.raises(ManifestValidationError, match="at least one camera"):
        parse_manifest(invalid)


def test_unknown_required_source_fails(manifest_data):
    invalid = deepcopy(manifest_data)
    invalid["recording"]["required_sources"].append("missing_camera")

    with pytest.raises(ManifestValidationError, match="missing_camera"):
        parse_manifest(invalid)


def test_camera_count_has_no_fixed_upper_bound(manifest_data):
    expanded = deepcopy(manifest_data)
    template = expanded["interfaces"]["cameras"][0]
    for index in range(20):
        camera = deepcopy(template)
        camera["key"] = f"monitor_{index}"
        camera["topic"] = f"/monitor/{index}/compressed"
        expanded["interfaces"]["cameras"].append(camera)

    manifest = parse_manifest(expanded)

    assert len(manifest.interfaces.cameras) == 25

