from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from robot_agent.local_health import (
    build_local_health_snapshot,
    parse_local_health_snapshot,
)
from robot_agent.manifest import load_manifest
from robot_agent.monitor import RobotMonitor
from sang_opcua_gateway.local_health_client import (
    LocalHealthReplayError,
    LocalHealthReplayStore,
)


ROOT = Path(__file__).parents[3]
MANIFEST = load_manifest(ROOT / "manifests/bi_so101_01.yaml")
KEY = b"phase-3-local-replay-test-key-32-bytes"
BOOT_A = "12345678-1234-5678-9234-567812345678"
BOOT_B = "87654321-4321-6789-9234-567812345678"
STAMP = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def snapshot(sequence, *, boot_id=BOOT_A, stamp=STAMP):
    robot = RobotMonitor(MANIFEST).robot_snapshot(now_monotonic=1.0)
    wire = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=boot_id,
        sequence=sequence,
        snapshot_time_utc=stamp.isoformat(),
        manifest=MANIFEST,
        robot=robot,
        signing_key=KEY,
        key_id="soarm-health-01",
    )
    return parse_local_health_snapshot(
        wire,
        expected_agent_id="workcell-soarm-01",
        manifest=MANIFEST,
        verification_key=KEY,
        expected_key_id="soarm-health-01",
        require_authenticated=True,
    )


def test_replay_high_water_survives_gateway_restart(tmp_path):
    path = tmp_path / "edge" / "health-replay.db"
    store = LocalHealthReplayStore(path)
    first = snapshot(1)
    second = snapshot(2, stamp=STAMP + timedelta(seconds=1))

    assert store.accept(first) is True
    assert store.accept(first) is False
    assert store.accept(second) is True
    assert path.stat().st_mode & 0o777 == 0o600

    restarted = LocalHealthReplayStore(path)
    with pytest.raises(LocalHealthReplayError, match="moved backwards"):
        restarted.accept(first)

    next_boot = snapshot(
        1,
        boot_id=BOOT_B,
        stamp=STAMP + timedelta(seconds=2),
    )
    assert restarted.accept(next_boot) is True
    with pytest.raises(LocalHealthReplayError, match="retired boot_id"):
        restarted.accept(snapshot(3, stamp=STAMP + timedelta(seconds=3)))


def test_replay_store_requires_authenticated_snapshot(tmp_path):
    robot = RobotMonitor(MANIFEST).robot_snapshot(now_monotonic=1.0)
    wire = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=BOOT_A,
        sequence=1,
        snapshot_time_utc=STAMP.isoformat(),
        manifest=MANIFEST,
        robot=robot,
    )
    unsigned = parse_local_health_snapshot(
        wire,
        expected_agent_id="workcell-soarm-01",
        manifest=MANIFEST,
    )

    with pytest.raises(LocalHealthReplayError, match="requires authenticated"):
        LocalHealthReplayStore(tmp_path / "replay.db").accept(unsigned)
