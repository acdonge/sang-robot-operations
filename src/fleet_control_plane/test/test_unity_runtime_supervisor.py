import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from fleet_control_plane.unity_runtime_supervisor import (
    UnityRuntimeSupervisor,
    create_runtime_app,
)


def fake_player(tmp_path: Path, *, isolated: bool = True) -> Path:
    player = tmp_path / "fixture-unity-player"
    player.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        "pathlib.Path(os.environ['SANG_FIXTURE_ENV']).write_text(\n"
        "    f\"rb5={os.environ.get('IITP_RB5_SIM_TO_REAL')}|\"\n"
        "    f\"omy={os.environ.get('IITP_OMY_SIM_TO_REAL')}|\"\n"
        "    f\"orchestrator={os.environ.get('SANG_UNITY_ORCHESTRATOR')}\")\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    player.chmod(0o755)
    Path(f"{player}.simulation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_role": "simulation_executor",
                "scenario_id": "fixture-scenario",
                "physical_output_connected": not isolated,
                "compile_define": "SANG_SIMULATION_PLAYER",
                "control_surface": "external_workflow_ui",
            }
        ),
        encoding="utf-8",
    )
    return player


def test_runtime_starts_only_fixed_disconnected_player_and_stops(tmp_path, monkeypatch):
    player = fake_player(tmp_path)
    bridge_root = tmp_path / "bridge"
    marker_path = tmp_path / "marker.json"
    fixture_env = tmp_path / "environment.txt"
    monkeypatch.setenv("SANG_FIXTURE_ENV", str(fixture_env))
    monkeypatch.setenv("IITP_RB5_SIM_TO_REAL", "1")
    monkeypatch.setenv("IITP_OMY_SIM_TO_REAL", "1")
    supervisor = UnityRuntimeSupervisor(
        player_path=player,
        bridge_root=bridge_root,
        marker_path=marker_path,
        log_path=tmp_path / "unity.log",
    )
    client = TestClient(create_runtime_app(supervisor))

    started = client.post("/start")
    assert started.status_code == 200
    assert started.json()["running"] is True
    assert started.json()["physical_output_connected"] is False
    assert client.post("/start").json()["idempotent_replay"] is True
    assert json.loads(marker_path.read_text(encoding="utf-8")) == {
        "bridge_root": str(bridge_root)
    }
    for _ in range(50):
        if fixture_env.exists():
            break
        time.sleep(0.01)
    assert fixture_env.read_text(encoding="utf-8") == (
        "rb5=None|omy=None|orchestrator=1"
    )

    stopped = client.post("/stop")
    assert stopped.status_code == 200
    assert stopped.json()["running"] is False
    assert client.post("/stop").json()["idempotent_replay"] is True


def test_runtime_rejects_non_isolated_build_manifest(tmp_path):
    with pytest.raises(ValueError, match="output-disconnected"):
        UnityRuntimeSupervisor(
            player_path=fake_player(tmp_path, isolated=False),
            bridge_root=tmp_path / "bridge",
            marker_path=tmp_path / "marker.json",
            log_path=tmp_path / "unity.log",
        )
