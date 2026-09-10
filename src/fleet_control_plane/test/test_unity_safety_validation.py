import json
from pathlib import Path

import pytest

from fleet_control_plane.task_bundle import load_task_bundle
from fleet_control_plane.unity_safety_validation import validate_unity_safety_project


ROOT = Path(__file__).parents[3]
BUNDLE_PATH = ROOT / "contracts" / "tasks" / "iitp-scenario1-shadow.v1.json"


def _write_unity_project(tmp_path, *, run_id="iitp-scenario1-shadow-dev-20260827"):
    project = tmp_path / "Scenario1_app"
    config_dir = project / "Assets" / "StreamingAssets"
    config_dir.mkdir(parents=True)
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir()
    (settings_dir / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.5.6f1\n",
        encoding="utf-8",
    )
    (config_dir / "sang_safety_shadow.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "desktop_only": True,
                "edge_endpoint": "http://127.0.0.1:18092/safety/v1/snapshots",
                "source_kind": "synthetic",
                "agent_id": "processing-lab1",
                "run_id": run_id,
                "coordinate_frame": "iitp-scenario1-unity-world-demo",
                "ttl_ms": 1500,
                "publish_interval_seconds": 0.5,
                "tracking_signal_asset_id": "iitp-s1-human-tracker-sim",
                "asset_ids": [
                    "iitp-s1-omy-sim",
                    "iitp-s1-rb5-sim",
                    "iitp-s1-human-tracker-sim",
                ],
                "zone_asset_ids": ["iitp-s1-omy-sim", "iitp-s1-rb5-sim"],
            }
        ),
        encoding="utf-8",
    )
    return project


def test_unity_project_matches_task_and_snapshot_contract(tmp_path):
    result = validate_unity_safety_project(
        load_task_bundle(BUNDLE_PATH),
        _write_unity_project(tmp_path),
    )

    assert result["status"] == "ok"
    assert result["source_kind"] == "synthetic"
    assert result["command_capability"] is False


def test_unity_project_rejects_task_run_drift(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        validate_unity_safety_project(
            load_task_bundle(BUNDLE_PATH),
            _write_unity_project(tmp_path, run_id="different-run"),
        )
