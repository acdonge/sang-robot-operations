import json

from fleet_control_plane.models import AgentHeartbeatV1
from robot_agent.recording_inventory import scan_recording_inventory


def heartbeat(recordings):
    return {
        "schema_version": 1,
        "agent_id": "processing-lab1",
        "agent_name": "Lab1",
        "sw_version": "0.1.0",
        "sent_at": "2026-08-06T12:00:00Z",
        "storage": {},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "recordings": recordings,
        "robots": [],
    }


def write_manifest(root, session_id, **overrides):
    directory = root / session_id
    directory.mkdir()
    payload = {
        "schema_version": "1.0",
        "session_id": session_id,
        "state": "complete",
        "profile_id": "profile-01",
        "agent_id": "processing-lab1",
        "robot_id": "robot-01",
        "run_id": None,
        "task_definition": None,
        "started_at": "2026-08-06T12:00:00Z",
        "finished_at": "2026-08-06T12:00:10Z",
        "storage_identifier": "mcap",
        "topics": [{"message_count": 10}],
        "total_size_bytes": 1024,
        "recorder_diagnostics": {"transport_messages_lost": 0},
        **overrides,
    }
    (directory / "session-manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_real_scanner_output_always_round_trips_central_contract(tmp_path):
    write_manifest(tmp_path, "valid-session")
    write_manifest(tmp_path, "long-profile", profile_id="x" * 65)
    write_manifest(
        tmp_path,
        "backwards-time",
        started_at="2026-08-06T13:00:00Z",
        finished_at="2026-08-06T12:00:00Z",
    )
    (tmp_path / "collision").mkdir()
    (tmp_path / "collision.partial").mkdir()

    inventory = scan_recording_inventory(
        tmp_path,
        agent_id="processing-lab1",
    )
    validated = AgentHeartbeatV1.model_validate(heartbeat(inventory))

    assert validated.recordings.complete_count == 1
    assert validated.recordings.invalid_count == 4
    assert len(validated.recordings.recent) == 4
