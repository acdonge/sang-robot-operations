import json
from pathlib import Path

from robot_agent.recording_inventory import (
    RecordingInventory,
    scan_recording_inventory,
)


AGENT_ID = "processing-lab1"


def write_manifest(
    root: Path,
    session_id: str,
    *,
    state: str,
    agent_id: str = AGENT_ID,
    lost: int = 0,
) -> Path:
    directory = root / (f"{session_id}.partial" if state == "failed" else session_id)
    directory.mkdir()
    payload = {
        "schema_version": "1.0",
        "session_id": session_id,
        "state": state,
        "profile_id": "rb5-lab1-observation-v1",
        "agent_id": agent_id,
        "robot_id": "rb5_lab1_eval_01",
        "run_id": "run-01",
        "task_definition": {"id": "weld-task", "version": 2},
        "started_at": "2026-08-06T14:44:01Z",
        "finished_at": "2026-08-06T14:44:11Z",
        "recorder_diagnostics": {"transport_messages_lost": lost},
    }
    if state == "complete":
        payload.update(
            {
                "storage_identifier": "mcap",
                "total_size_bytes": 1_024,
                "topics": [
                    {"name": "/joints", "message_count": 20},
                    {"name": "/camera", "message_count": 30},
                ],
            }
        )
    (directory / "session-manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return directory


def test_inventory_reports_complete_failed_and_partial_metadata(tmp_path):
    complete = write_manifest(tmp_path, "capture-001", state="complete")
    failed = write_manifest(tmp_path, "capture-002", state="failed", lost=14)
    partial = tmp_path / "capture-003.partial"
    partial.mkdir()
    complete.touch()
    failed.touch()
    partial.touch()

    inventory = scan_recording_inventory(tmp_path, agent_id=AGENT_ID)

    assert inventory["state"] == "ready"
    assert inventory["complete_count"] == 1
    assert inventory["failed_count"] == 1
    assert inventory["partial_count"] == 1
    assert inventory["invalid_count"] == 0
    assert inventory["scanned_count"] == 3
    assert inventory["total_size_bytes"] == 1_024
    sessions = {item["session_id"]: item for item in inventory["recent"]}
    assert sessions["capture-001"]["message_count"] == 50
    assert sessions["capture-001"]["topic_count"] == 2
    assert sessions["capture-001"]["task_definition_id"] == "weld-task"
    assert sessions["capture-002"]["transport_messages_lost"] == 14
    assert sessions["capture-003"]["state"] == "partial"


def test_recent_sessions_use_manifest_time_not_directory_mtime(tmp_path):
    older = write_manifest(tmp_path, "capture-older", state="complete")
    newer = write_manifest(tmp_path, "capture-newer", state="complete")
    older_manifest = older / "session-manifest.json"
    newer_manifest = newer / "session-manifest.json"
    older_payload = json.loads(older_manifest.read_text(encoding="utf-8"))
    newer_payload = json.loads(newer_manifest.read_text(encoding="utf-8"))
    older_payload["finished_at"] = "2026-08-06T14:44:11Z"
    newer_payload["finished_at"] = "2026-08-06T15:44:11Z"
    older_manifest.write_text(json.dumps(older_payload), encoding="utf-8")
    newer_manifest.write_text(json.dumps(newer_payload), encoding="utf-8")
    newer.touch()
    older.touch()

    inventory = scan_recording_inventory(tmp_path, agent_id=AGENT_ID)

    assert [item["session_id"] for item in inventory["recent"]] == [
        "capture-newer",
        "capture-older",
    ]


def test_inventory_isolates_malformed_cross_agent_and_symlink_sessions(tmp_path):
    malformed = tmp_path / "capture-bad"
    malformed.mkdir()
    (malformed / "session-manifest.json").write_text("{", encoding="utf-8")
    write_manifest(
        tmp_path,
        "capture-other",
        state="complete",
        agent_id="another-agent",
    )
    target = tmp_path / ".outside"
    target.mkdir()
    (tmp_path / "capture-link").symlink_to(target, target_is_directory=True)
    broken_manifest = tmp_path / "capture-broken.partial"
    broken_manifest.mkdir()
    (broken_manifest / "session-manifest.json").symlink_to(
        tmp_path / "missing-manifest.json"
    )

    inventory = scan_recording_inventory(tmp_path, agent_id=AGENT_ID)

    assert inventory["state"] == "degraded"
    assert inventory["error_code"] == "invalid_session_metadata"
    assert inventory["invalid_count"] == 4
    assert {item["state"] for item in inventory["recent"]} == {"invalid"}


def test_inventory_isolates_values_that_central_contract_would_reject(tmp_path):
    long_id = "x" * 65
    long_profile = write_manifest(tmp_path, "capture-long", state="complete")
    long_payload_path = long_profile / "session-manifest.json"
    long_payload = json.loads(long_payload_path.read_text(encoding="utf-8"))
    long_payload["profile_id"] = long_id
    long_payload_path.write_text(json.dumps(long_payload), encoding="utf-8")

    backwards = write_manifest(tmp_path, "capture-backwards", state="complete")
    backwards_path = backwards / "session-manifest.json"
    backwards_payload = json.loads(backwards_path.read_text(encoding="utf-8"))
    backwards_payload["started_at"] = "2026-08-06T15:00:00Z"
    backwards_payload["finished_at"] = "2026-08-06T14:00:00Z"
    backwards_path.write_text(json.dumps(backwards_payload), encoding="utf-8")

    inventory = scan_recording_inventory(tmp_path, agent_id=AGENT_ID)

    assert inventory["invalid_count"] == 2
    assert all(item["state"] == "invalid" for item in inventory["recent"])


def test_final_and_partial_collision_is_invalid_without_duplicate_recent_id(tmp_path):
    write_manifest(tmp_path, "capture-collision", state="complete")
    partial = tmp_path / "capture-collision.partial"
    partial.mkdir()

    inventory = scan_recording_inventory(tmp_path, agent_id=AGENT_ID)

    assert inventory["invalid_count"] == 2
    assert inventory["scanned_count"] == 2
    assert [item["session_id"] for item in inventory["recent"]] == [
        "capture-collision"
    ]
    assert inventory["recent"][0]["state"] == "invalid"


def test_inventory_bounds_scan_and_recent_payload(tmp_path):
    for index in range(5):
        directory = write_manifest(
            tmp_path,
            f"capture-{index:03d}",
            state="complete",
        )
        directory.touch()

    inventory = scan_recording_inventory(
        tmp_path,
        agent_id=AGENT_ID,
        scan_limit=3,
        recent_limit=2,
    )

    assert inventory["truncated"] is True
    assert inventory["scanned_count"] == 3
    assert len(inventory["recent"]) == 2


def test_missing_or_symlink_capture_root_is_degraded(tmp_path):
    missing = scan_recording_inventory(tmp_path / "missing", agent_id=AGENT_ID)
    assert missing["state"] == "degraded"
    assert missing["error_code"] == "capture_root_unavailable"

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "captures"
    link.symlink_to(target, target_is_directory=True)
    linked = scan_recording_inventory(link, agent_id=AGENT_ID)
    assert linked["state"] == "degraded"
    assert linked["error_code"] == "capture_root_unavailable"


def test_inventory_cache_avoids_reparsing_until_refresh(tmp_path):
    inventory = RecordingInventory(
        tmp_path,
        agent_id=AGENT_ID,
        refresh_interval_s=10.0,
    )
    first = inventory.sample(now_monotonic=1.0)
    write_manifest(tmp_path, "capture-new", state="complete")
    cached = inventory.sample(now_monotonic=5.0)
    refreshed = inventory.sample(now_monotonic=12.0)

    assert first["complete_count"] == 0
    assert cached["complete_count"] == 0
    assert refreshed["complete_count"] == 1


def test_disabled_inventory_reports_no_capability():
    inventory = RecordingInventory(None, agent_id=AGENT_ID)

    assert inventory.sample()["enabled"] is False
    assert inventory.sample()["state"] == "disabled"
