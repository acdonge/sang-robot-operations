from pathlib import Path
from types import SimpleNamespace

from recorder_core.manifest import load_manifest
from recorder_core.validate_cli import _validate_metadata
from recorder_core.writer import features_from_manifest


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def test_validator_accepts_multiple_nonempty_episodes():
    manifest = load_manifest(MANIFEST_PATH)
    dataset = SimpleNamespace(
        fps=manifest.recording.fps,
        num_frames=20,
        num_episodes=2,
        features=features_from_manifest(manifest),
    )
    errors = []

    _validate_metadata(dataset, manifest, errors)

    assert errors == []
