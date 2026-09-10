from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from fleet_control_plane.safety_edge import create_safety_edge_app
from fleet_control_plane.safety_models import SafetyPolicyV1


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def policy():
    return SafetyPolicyV1(
        policy_id="iitp-scenario1-shadow",
        version=1,
        warning_distance_m=1.0,
        unsafe_distance_m=0.5,
        max_signal_age_ms=1000,
    )


def snapshot(*, distance=1.5, valid=True, occupancies=None):
    return {
        "schema_version": 1,
        "snapshot_id": "unity-snapshot-001",
        "source_kind": "synthetic",
        "agent_id": "processing-lab1",
        "run_id": "iitp-scenario1-shadow-dev-20260827",
        "captured_at": NOW.isoformat(),
        "ttl_ms": 1500,
        "coordinate_frame": "iitp-scenario1-unity-world-demo",
        "clock_quality": "synchronized",
        "asset_ids": [
            "iitp-s1-omy-sim",
            "iitp-s1-rb5-sim",
            "iitp-s1-human-tracker-sim",
        ],
        "signals": [
            {
                "signal_id": "xr-human-tracking",
                "sampled_at": NOW.isoformat(),
                "asset_id": "iitp-s1-human-tracker-sim",
                "required": True,
                "valid": valid,
                "confidence": 0.95,
            }
        ],
        "human_robot_distance_m": distance,
        "tracking_confidence": 0.95,
        "forbidden_zone_occupancies": occupancies or [],
        "task_progress": {
            "expected_step": "rb5_waam",
            "observed_step": "rb5_waam",
        },
        "evidence_refs": [],
    }


def evidence_v2():
    return {
        "schema_version": 2,
        "snapshot_id": "unity-v2-001",
        "producer_id": "unity-observer",
        "producer_boot_id": "11111111-1111-4111-8111-111111111111",
        "sequence": 1,
        "run_id": "iitp-scenario1-shadow-dev-20260827",
        "captured_at": NOW.isoformat(),
        "asset_ids": ["iitp-s1-rb5-sim"],
        "manifest_refs": [
            {
                "asset_id": "iitp-s1-rb5-sim",
                "manifest_ref": {
                    "manifest_id": "rb5-sim-manifest",
                    "version": 1,
                    "sha256": "a" * 64,
                },
            }
        ],
        "policy_ref": {
            "object_id": "scenario1-safety-policy",
            "version": 1,
            "sha256": "b" * 64,
        },
        "signals": [
            {
                "signal_id": "guard-clear",
                "signal_type": "protective-zone-clear",
                "sampled_at": NOW.isoformat(),
                "asset_ids": ["iitp-s1-rb5-sim"],
                "value": True,
                "required": True,
                "valid": True,
                "confidence": 0.99,
            }
        ],
        "command_emitted": False,
    }


def test_edge_evaluates_and_forwards_without_command_capability():
    forwarded = []

    def submit(assessment):
        forwarded.append(assessment)
        return {"accepted": True}

    client = TestClient(
        create_safety_edge_app(
            policy(),
            submit,
            now=lambda: NOW,
            measured_clock_quality=lambda: "synchronized",
        )
    )
    response = client.post("/safety/v1/snapshots", json=snapshot())

    assert response.status_code == 202, response.text
    assessment = response.json()["assessment"]
    assert assessment["state"] == "SAFE"
    assert assessment["shadow_mode"] is True
    assert assessment["command_emitted"] is False
    assert len(forwarded) == 1
    assert client.get("/healthz").json()["command_capability"] is False


def test_edge_preserves_reference_evaluator_precedence():
    client = TestClient(
        create_safety_edge_app(
            policy(),
            lambda assessment: {},
            now=lambda: NOW,
            measured_clock_quality=lambda: "synchronized",
        )
    )
    unsafe = snapshot(
        distance=0.2,
        occupancies=[
            {
                "zone_id": "robot-work-envelope",
                "entity_id": "xr-headset-user",
                "asset_ids": ["iitp-s1-rb5-sim"],
            }
        ],
    )
    unknown_and_unsafe = client.post(
        "/safety/v1/snapshots",
        json={**unsafe, "signals": [{**unsafe["signals"][0], "valid": False}]},
    ).json()["assessment"]

    assert unknown_and_unsafe["state"] == "UNSAFE"
    assert {reason["severity"] for reason in unknown_and_unsafe["reasons"]} == {
        "UNKNOWN",
        "UNSAFE",
    }


def test_edge_validates_and_forwards_v2_without_adding_trust_fields():
    forwarded = []

    def submit_v2(evidence):
        forwarded.append(evidence)
        return {"state": "SAFE"}

    client = TestClient(
        create_safety_edge_app(
            policy(),
            lambda assessment: {},
            submit_v2=submit_v2,
            now=lambda: NOW,
        )
    )
    response = client.post("/safety/v2/evidence", json=evidence_v2())

    assert response.status_code == 202, response.text
    assert response.json()["command_emitted"] is False
    assert len(forwarded) == 1
    encoded = forwarded[0].model_dump(mode="json")
    assert "received_at" not in encoded
    assert "clock" not in encoded
    assert "source_kind" not in encoded


def test_edge_rejects_oversized_or_invalid_snapshots():
    client = TestClient(
        create_safety_edge_app(
            policy(),
            lambda assessment: {},
            now=lambda: NOW,
            measured_clock_quality=lambda: "synchronized",
        )
    )
    invalid = client.post("/safety/v1/snapshots", json={"schema_version": 1})
    assert invalid.status_code == 422

    oversized = client.post(
        "/safety/v1/snapshots",
        content=b"x" * (256 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
