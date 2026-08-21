from datetime import datetime, timezone

import pytest

from fleet_control_plane.models import AgentHeartbeatV1


def heartbeat(*, source_rate_ok, robot_rates_ok):
    return {
        "schema_version": 1,
        "agent_id": "workcell-soarm-01",
        "agent_name": "SO-ARM Workcell",
        "sw_version": "0.1.0",
        "sent_at": datetime(2026, 8, 4, tzinfo=timezone.utc).isoformat(),
        "storage": {},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "robots": [
            {
                "robot_id": "bi_so101_01",
                "model": "bi_so101",
                "fsm_state": "DEGRADED",
                "phase": "MONITORING",
                "sources_fresh": True,
                "rates_ok": robot_rates_ok,
                "sources": [
                    {
                        "key": "observation.state",
                        "kind": "state",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": source_rate_ok,
                        "topics": [
                            {
                                "topic": "/joint_states",
                                "age_ms": 1.0,
                                "rate_hz": None,
                                "minimum_rate_hz": 18.0,
                                "rate_ok": None,
                                "seq": 1,
                                "fresh": True,
                            }
                        ],
                    }
                ],
                "fault": "low-rate required sources: observation.state",
                "recording": {
                    "active": False,
                    "episode_index": 0,
                    "episodes_total": 0,
                    "frames": 0,
                    "elapsed_s": 0.0,
                    "current_instruction": None,
                },
            }
        ],
    }


def test_required_configured_rate_is_not_ok_until_it_is_measurable():
    validated = AgentHeartbeatV1.model_validate(
        heartbeat(source_rate_ok=None, robot_rates_ok=False)
    )
    assert validated.robots[0].rates_ok is False

    with pytest.raises(ValueError, match="configured required source rates"):
        AgentHeartbeatV1.model_validate(
            heartbeat(source_rate_ok=None, robot_rates_ok=True)
        )


def test_recording_inventory_is_additive_and_validated():
    legacy = AgentHeartbeatV1.model_validate(
        heartbeat(source_rate_ok=None, robot_rates_ok=False)
    )
    assert legacy.recordings.state == "disabled"

    payload = heartbeat(source_rate_ok=None, robot_rates_ok=False)
    payload["recordings"] = {
        "enabled": True,
        "state": "ready",
        "error_code": None,
        "complete_count": 1,
        "failed_count": 0,
        "partial_count": 0,
        "invalid_count": 0,
        "scanned_count": 1,
        "total_size_bytes": 1024,
        "truncated": False,
        "recent": [
            {
                "session_id": "capture-01",
                "state": "complete",
                "profile_id": "profile-01",
                "robot_id": "bi_so101_01",
                "started_at": "2026-08-06T12:00:00Z",
                "finished_at": "2026-08-06T12:00:10Z",
                "total_size_bytes": 1024,
                "topic_count": 3,
                "message_count": 100,
                "transport_messages_lost": 0,
            }
        ],
    }
    validated = AgentHeartbeatV1.model_validate(payload)
    assert validated.recordings.recent[0].topic_count == 3

    payload["recordings"]["scanned_count"] = 2
    with pytest.raises(ValueError, match="scanned_count"):
        AgentHeartbeatV1.model_validate(payload)


def test_heartbeat_versions_pin_runtime_identity_rules():
    payload = heartbeat(source_rate_ok=None, robot_rates_ok=False)
    payload.update(
        {
            "schema_version": 3,
            "boot_id": "019fce1c-a4b5-7ba2-b0d5-01ea4940926d",
            "sequence": 1,
        }
    )
    payload["robots"][0]["runtime_kind"] = "physical"

    validated = AgentHeartbeatV1.model_validate(payload)
    assert validated.robots[0].runtime_kind == "physical"

    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="must not contain runtime_kind"):
        AgentHeartbeatV1.model_validate(payload)

    payload["robots"][0]["runtime_kind"] = None
    with pytest.raises(ValueError, match="must not contain runtime_kind"):
        AgentHeartbeatV1.model_validate(payload)

    payload["schema_version"] = 99
    del payload["robots"][0]["runtime_kind"]
    with pytest.raises(ValueError, match="unsupported heartbeat schema_version"):
        AgentHeartbeatV1.model_validate(payload)
