from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from robot_agent.local_health import (
    LocalHealthError,
    build_local_health_snapshot,
    load_mapping_health_digest,
    manifest_health_digest,
    parse_local_health_snapshot,
)
from robot_agent.manifest import load_manifest
from robot_agent.monitor import RobotMonitor


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"
MAPPING_PATH = (
    Path(__file__).parents[3]
    / "contracts"
    / "opcua"
    / "bi_so101_01.mapping.v2.yaml"
)
BOOT_ID = "12345678-1234-5678-9234-567812345678"
SIGNING_KEY = b"phase-3-local-health-test-key-32-bytes"


def robot_snapshot():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    for source in monitor.manifest.sources:
        if not source.required:
            continue
        for topic in source.topics:
            monitor.mark(
                source.key,
                topic,
                received_monotonic=10.0,
                rate_timestamp_s=100.0,
            )
            monitor.mark(
                source.key,
                topic,
                received_monotonic=10.03,
                rate_timestamp_s=100.03,
            )
    return monitor.robot_snapshot(now_monotonic=10.04)


def wire_snapshot():
    manifest = load_manifest(MANIFEST_PATH)
    return build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=BOOT_ID,
        sequence=7,
        snapshot_time_utc=datetime(
            2026,
            8,
            4,
            12,
            0,
            tzinfo=timezone.utc,
        ).isoformat(),
        manifest=manifest,
        robot=robot_snapshot(),
    )


def canonical_digest(value):
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_local_health_snapshot_round_trip_is_versioned_and_manifest_bound():
    manifest = load_manifest(MANIFEST_PATH)
    payload = wire_snapshot()
    parsed = parse_local_health_snapshot(
        json.dumps(payload).encode("utf-8"),
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
    )

    assert parsed.boot_id == BOOT_ID
    assert parsed.sequence == 7
    assert parsed.manifest_digest == manifest_health_digest(manifest)
    assert parsed.health.fsm_state == "READY"
    assert parsed.health.sources["observation.state"].rate_ok is True
    assert parsed.health.sources["action"].rate_ok is None


def test_local_health_manifest_digest_binds_mapping_v2_metadata():
    manifest = load_manifest(MANIFEST_PATH)
    mapping_digest = load_mapping_health_digest(
        MAPPING_PATH,
        expected_robot_id=manifest.robot.robot_id,
    )
    payload = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=BOOT_ID,
        sequence=8,
        snapshot_time_utc=datetime(
            2026,
            8,
            4,
            12,
            0,
            tzinfo=timezone.utc,
        ).isoformat(),
        manifest=manifest,
        robot=robot_snapshot(),
        mapping_digest=mapping_digest,
    )
    parsed = parse_local_health_snapshot(
        payload,
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
        expected_mapping_digest=mapping_digest,
    )

    assert parsed.mapping_digest == mapping_digest
    assert parsed.manifest_digest == manifest_health_digest(
        manifest,
        mapping_digest,
    )
    with pytest.raises(LocalHealthError, match="manifest_digest mismatch"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
        )


def test_authenticated_snapshot_rejects_unsigned_wrong_key_and_tampering():
    manifest = load_manifest(MANIFEST_PATH)
    payload = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=BOOT_ID,
        sequence=8,
        snapshot_time_utc=datetime(
            2026,
            8,
            4,
            12,
            0,
            tzinfo=timezone.utc,
        ).isoformat(),
        manifest=manifest,
        robot=robot_snapshot(),
        signing_key=SIGNING_KEY,
        key_id="soarm-health-01",
    )
    parsed = parse_local_health_snapshot(
        payload,
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
        verification_key=SIGNING_KEY,
        expected_key_id="soarm-health-01",
        require_authenticated=True,
    )
    assert parsed.authenticated is True
    assert parsed.key_id == "soarm-health-01"

    with pytest.raises(LocalHealthError, match="verification key"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
            require_authenticated=True,
        )
    with pytest.raises(LocalHealthError, match="signature mismatch"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
            verification_key=b"another-valid-test-key-with-32-bytes",
            expected_key_id="soarm-health-01",
            require_authenticated=True,
        )

    tampered = deepcopy(payload)
    tampered["health"]["rates_ok"] = False
    with pytest.raises(LocalHealthError, match="signature mismatch"):
        parse_local_health_snapshot(
            tampered,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
            verification_key=SIGNING_KEY,
            expected_key_id="soarm-health-01",
            require_authenticated=True,
        )

    with pytest.raises(LocalHealthError, match="authenticated.*required"):
        parse_local_health_snapshot(
            wire_snapshot(),
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
            verification_key=SIGNING_KEY,
            expected_key_id="soarm-health-01",
            require_authenticated=True,
        )


def test_payload_tamper_and_semantically_inconsistent_health_are_rejected():
    manifest = load_manifest(MANIFEST_PATH)
    payload = wire_snapshot()
    payload["health"]["rates_ok"] = False

    with pytest.raises(LocalHealthError, match="payload_digest"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
        )

    payload["payload_digest"] = canonical_digest(payload["health"])
    with pytest.raises(LocalHealthError, match="rates_ok is inconsistent"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
        )


def test_identity_sequence_and_manifest_digest_are_strict():
    manifest = load_manifest(MANIFEST_PATH)
    payload = wire_snapshot()

    with pytest.raises(LocalHealthError, match="agent_id mismatch"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="other-agent",
            manifest=manifest,
        )

    payload = deepcopy(payload)
    payload["sequence"] = 0
    with pytest.raises(LocalHealthError, match="positive integer"):
        parse_local_health_snapshot(
            payload,
            expected_agent_id="workcell-soarm-01",
            manifest=manifest,
        )
