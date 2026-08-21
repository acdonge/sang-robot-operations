from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from robot_agent.manifest import load_manifest
from robot_agent.local_health import (
    build_local_health_snapshot,
    parse_local_health_snapshot,
)
from robot_agent.monitor import RobotMonitor
from sang_opcua_gateway.contract import load_gateway_contract
from sang_opcua_gateway.state import GatewayStore


ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc
LEFT_NAMES = (
    "left_shoulder_pan",
    "left_shoulder_lift",
    "left_elbow_flex",
    "left_wrist_flex",
    "left_wrist_roll",
    "left_gripper",
)
LEFT_TOPIC = "/bi_so101/follower_left/joint_states_norm"
ACTION_TOPIC = "/bi_so101/leader_left/joint_trajectory_norm"
CAMERA_TOPIC = "/bi_so101/camera/top/color/image_rect_raw/compressed"


def contract(mapping_path=None):
    return load_gateway_contract(
        manifest_path=ROOT / "manifests/bi_so101_01.yaml",
        mapping_path=(
            mapping_path
            or ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
        ),
        model_contract_path=ROOT / "contracts/opcua/model-contract.yaml",
        status_codes_path=ROOT / "contracts/opcua/status-code-mapping.yaml",
        nodeset_path=ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        endpoint="opc.tcp://127.0.0.1:4840/sang/",
        agent_id="workcell-soarm-01",
    )


def test_valid_joint_value_preserves_ros_source_timestamp():
    store = GatewayStore(contract(), started_monotonic=0.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    assert store.ingest_joint(
        LEFT_TOPIC,
        LEFT_NAMES,
        (-10.0, -5.0, 0.0, 5.0, 10.0, 50.0),
        source_timestamp=stamp,
        received_monotonic=1.0,
        received_utc=stamp + timedelta(milliseconds=2),
    )
    snapshot = store.snapshot(now_monotonic=1.01, now_utc=stamp)
    feature = snapshot.sources["observation.state"].topics[LEFT_TOPIC].features[
        "left_gripper"
    ]

    assert feature.value == 50.0
    assert feature.status == "Good"
    assert feature.source_timestamp == stamp
    assert feature.provenance == "ros_header"


def test_zero_header_falls_back_to_receive_time_with_uncertain_quality():
    store = GatewayStore(contract(), started_monotonic=0.0)
    received = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    store.ingest_joint(
        LEFT_TOPIC,
        LEFT_NAMES,
        (0.0, 0.0, 0.0, 0.0, 0.0, 20.0),
        received_monotonic=1.0,
        received_utc=received,
    )
    feature = store.snapshot(now_monotonic=1.01, now_utc=received).sources[
        "observation.state"
    ].topics[LEFT_TOPIC].features["left_gripper"]

    assert feature.status == "Uncertain"
    assert feature.source_timestamp == received
    assert feature.provenance == "gateway_receive_fallback"


def test_invalid_joint_message_clears_value_and_reports_decoding_error():
    store = GatewayStore(contract(), started_monotonic=0.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    assert not store.ingest_joint(
        LEFT_TOPIC,
        tuple(reversed(LEFT_NAMES)),
        (0.0,) * 6,
        source_timestamp=stamp,
        received_monotonic=1.0,
        received_utc=stamp,
    )
    feature = store.snapshot(now_monotonic=1.01, now_utc=stamp).sources[
        "observation.state"
    ].topics[LEFT_TOPIC].features["left_gripper"]

    assert feature.value is None
    assert feature.status == "BadDecodingError"


def test_stale_value_is_retained_with_last_usable_status():
    store = GatewayStore(contract(), started_monotonic=0.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    store.ingest_joint(
        LEFT_TOPIC,
        LEFT_NAMES,
        (0.0,) * 6,
        source_timestamp=stamp,
        received_monotonic=1.0,
        received_utc=stamp,
    )

    feature = store.snapshot(now_monotonic=2.01, now_utc=stamp).sources[
        "observation.state"
    ].topics[LEFT_TOPIC].features["left_shoulder_pan"]
    assert feature.value == 0.0
    assert feature.status == "UncertainLastUsableValue"


def test_optional_action_waits_without_degrading_robot_for_that_source():
    store = GatewayStore(contract(), started_monotonic=0.0)
    snapshot = store.snapshot(
        now_monotonic=4.0,
        now_utc=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )

    action = snapshot.sources["action"].topics[ACTION_TOPIC]
    assert action.features["left_gripper"].status == "BadWaitingForInitialData"
    assert snapshot.fsm_state == "DEGRADED"
    assert snapshot.rates_ok is False
    assert "action" not in snapshot.fault


def test_camera_store_tracks_only_metadata_not_payload():
    store = GatewayStore(contract(), started_monotonic=0.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    assert store.ingest_camera(
        CAMERA_TOPIC,
        "jpeg",
        source_timestamp=stamp,
        received_monotonic=1.0,
        received_utc=stamp,
    )

    topic = store.snapshot(now_monotonic=1.01, now_utc=stamp).sources["top"].topics[
        CAMERA_TOPIC
    ]
    assert topic.sequence == 1
    assert topic.sample.value == 1
    assert not hasattr(topic, "data")


def test_agent_and_gateway_share_required_health_semantics():
    gateway = GatewayStore(contract(), started_monotonic=0.0)
    monitor = RobotMonitor(load_manifest(ROOT / "manifests/bi_so101_01.yaml"))
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    for received_at in (10.0, 10.03):
        sample_stamp = stamp + timedelta(seconds=received_at - 10.0)
        for source in gateway.contract.sources:
            if not source.required:
                continue
            for topic in source.topics:
                if source.kind == "state":
                    assert gateway.ingest_joint(
                        topic.topic,
                        topic.features,
                        (0.0,) * len(topic.features),
                        source_timestamp=sample_stamp,
                        received_monotonic=received_at,
                        received_utc=stamp,
                    )
                else:
                    assert gateway.ingest_camera(
                        topic.topic,
                        "jpeg",
                        source_timestamp=sample_stamp,
                        received_monotonic=received_at,
                        received_utc=stamp,
                    )
                monitor.mark(
                    source.key,
                    topic.topic,
                    valid=True,
                    received_monotonic=received_at,
                    rate_timestamp_s=sample_stamp.timestamp(),
                )

    gateway_ready = gateway.snapshot(now_monotonic=10.04, now_utc=stamp)
    agent_ready = monitor.robot_snapshot(now_monotonic=10.04)
    assert gateway_ready.fsm_state == agent_ready["fsm_state"] == "READY"
    assert gateway_ready.sources_fresh == agent_ready["sources_fresh"] is True
    assert gateway_ready.rates_ok == agent_ready["rates_ok"] is True

    assert not gateway.ingest_joint(
        LEFT_TOPIC,
        tuple(reversed(LEFT_NAMES)),
        (0.0,) * len(LEFT_NAMES),
        source_timestamp=stamp + timedelta(seconds=0.05),
        received_monotonic=10.05,
        received_utc=stamp,
    )
    monitor.mark(
        "observation.state",
        LEFT_TOPIC,
        valid=False,
        received_monotonic=10.05,
        rate_timestamp_s=(stamp + timedelta(seconds=0.05)).timestamp(),
    )
    gateway_invalid = gateway.snapshot(now_monotonic=10.05, now_utc=stamp)
    agent_invalid = monitor.robot_snapshot(now_monotonic=10.05)
    gateway_topic = gateway_invalid.sources["observation.state"].topics[LEFT_TOPIC]
    agent_topic = agent_invalid["sources"][0]["topics"][0]

    assert gateway_topic.sequence == agent_topic["seq"] == 3
    assert gateway_topic.rate_ok == agent_topic["rate_ok"] is True
    assert gateway_topic.fresh == agent_topic["fresh"] is False
    assert gateway_invalid.fsm_state == agent_invalid["fsm_state"] == "DEGRADED"


def test_out_of_range_value_is_data_bad_but_transport_health_remains_fresh():
    store = GatewayStore(contract(), started_monotonic=0.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    assert not store.ingest_joint(
        LEFT_TOPIC,
        LEFT_NAMES,
        (101.0, 0.0, 0.0, 0.0, 0.0, 20.0),
        source_timestamp=stamp,
        received_monotonic=10.0,
        received_utc=stamp,
    )
    topic = store.snapshot(now_monotonic=10.01, now_utc=stamp).sources[
        "observation.state"
    ].topics[LEFT_TOPIC]

    assert topic.fresh is True
    assert topic.features["left_shoulder_pan"].status == "BadDecodingError"


def test_feature_range_enforcement_comes_from_mapping_v2(tmp_path):
    mapping_path = ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mapping["sources"][0]["topics"][0]["features"][0]["valid_range"] = [
        -1.0,
        1.0,
    ]
    narrowed_path = tmp_path / "narrowed.mapping.yaml"
    narrowed_path.write_text(
        yaml.safe_dump(mapping, sort_keys=False),
        encoding="utf-8",
    )
    names = LEFT_NAMES
    values = (2.0, 0.0, 0.0, 0.0, 0.0, 20.0)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    assert GatewayStore(contract()).ingest_joint(
        LEFT_TOPIC,
        names,
        values,
        source_timestamp=stamp,
    )
    assert not GatewayStore(contract(narrowed_path)).ingest_joint(
        LEFT_TOPIC,
        names,
        values,
        source_timestamp=stamp,
    )


def authoritative_snapshot(
    *,
    sequence=1,
    snapshot_time=None,
    boot_id="12345678-1234-5678-9234-567812345678",
):
    gateway_contract = contract()
    manifest = load_manifest(ROOT / "manifests/bi_so101_01.yaml")
    monitor = RobotMonitor(manifest)
    for source in manifest.sources:
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
    robot = monitor.robot_snapshot(now_monotonic=10.04)
    stamp = snapshot_time or datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    payload = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id=boot_id,
        sequence=sequence,
        snapshot_time_utc=stamp.isoformat(),
        manifest=manifest,
        robot=robot,
        mapping_digest=gateway_contract.mapping_digest,
    )
    return parse_local_health_snapshot(
        payload,
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
        expected_mapping_digest=gateway_contract.mapping_digest,
    )


def test_authoritative_agent_health_is_projected_without_masking_local_data_status():
    store = GatewayStore(
        contract(),
        started_monotonic=0.0,
        authoritative_health_timeout_s=12.0,
    )
    snapshot = authoritative_snapshot()
    assert store.apply_authoritative_health(
        snapshot,
        received_monotonic=10.0,
        received_utc=snapshot.snapshot_time,
    )

    projected = store.snapshot(
        now_monotonic=10.1,
        now_utc=datetime(2026, 8, 4, 12, 1, tzinfo=UTC),
    )
    state = projected.sources["observation.state"]

    assert projected.evaluated_at == snapshot.snapshot_time
    assert projected.health_evaluator_available is True
    assert projected.fsm_state == "READY"
    assert projected.sources_fresh is True
    assert projected.rates_ok is True
    assert state.fresh is True
    assert state.topics[LEFT_TOPIC].sequence == 2
    assert state.topics[LEFT_TOPIC].features["left_gripper"].status == (
        "BadNoCommunication"
    )


def test_authoritative_health_rejects_missing_mapping_v2_binding():
    gateway_contract = contract()
    manifest = gateway_contract.manifest
    monitor = RobotMonitor(manifest)
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    payload = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id="12345678-1234-5678-9234-567812345678",
        sequence=1,
        snapshot_time_utc=stamp.isoformat(),
        manifest=manifest,
        robot=monitor.robot_snapshot(now_monotonic=1.0),
    )
    legacy_snapshot = parse_local_health_snapshot(
        payload,
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="mapping_digest mismatch"):
        GatewayStore(gateway_contract).apply_authoritative_health(
            legacy_snapshot,
            received_utc=stamp,
        )


def test_authoritative_snapshot_stales_and_sequence_rollback_is_rejected():
    store = GatewayStore(
        contract(),
        started_monotonic=0.0,
        authoritative_health_timeout_s=12.0,
    )
    first = authoritative_snapshot(sequence=2)
    assert store.apply_authoritative_health(
        first,
        received_monotonic=10.0,
        received_utc=first.snapshot_time,
    )
    assert not store.apply_authoritative_health(
        first,
        received_monotonic=11.0,
        received_utc=first.snapshot_time + timedelta(seconds=1),
    )

    stale = store.snapshot(
        now_monotonic=22.1,
        now_utc=datetime(2026, 8, 4, 12, 1, tzinfo=UTC),
    )
    assert stale.fsm_state == "DEGRADED"
    assert stale.health_evaluator_available is False
    assert stale.sources_fresh is False
    assert stale.fault == "authoritative health snapshot unavailable"

    with pytest.raises(ValueError, match="sequence moved backwards"):
        store.apply_authoritative_health(
            authoritative_snapshot(sequence=1),
            received_monotonic=22.2,
            received_utc=first.snapshot_time + timedelta(seconds=2),
        )

    restarted = authoritative_snapshot(
        sequence=1,
        snapshot_time=datetime(2026, 8, 4, 12, 1, tzinfo=UTC),
        boot_id="87654321-4321-6789-9234-567812345678",
    )
    assert store.apply_authoritative_health(
        restarted,
        received_monotonic=22.3,
        received_utc=restarted.snapshot_time,
    )
    with pytest.raises(ValueError, match="boot_id was retired"):
        store.apply_authoritative_health(
            authoritative_snapshot(sequence=3),
            received_monotonic=22.4,
            received_utc=first.snapshot_time + timedelta(seconds=3),
        )


def test_authoritative_snapshot_wall_clock_age_is_bounded_after_restart():
    timeout_s = 12.0
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    old_store = GatewayStore(
        contract(),
        authoritative_health_timeout_s=timeout_s,
    )
    with pytest.raises(ValueError, match="too old"):
        old_store.apply_authoritative_health(
            authoritative_snapshot(snapshot_time=stamp),
            received_utc=stamp + timedelta(seconds=timeout_s + 0.001),
        )

    future_store = GatewayStore(
        contract(),
        authoritative_health_timeout_s=timeout_s,
    )
    with pytest.raises(ValueError, match="too far in the future"):
        future_store.apply_authoritative_health(
            authoritative_snapshot(snapshot_time=stamp),
            received_utc=stamp - timedelta(seconds=timeout_s + 0.001),
        )


def test_late_authoritative_snapshot_only_keeps_its_remaining_wall_clock_ttl():
    timeout_s = 12.0
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    store = GatewayStore(
        contract(),
        started_monotonic=0.0,
        authoritative_health_timeout_s=timeout_s,
    )
    snapshot = authoritative_snapshot(snapshot_time=stamp)
    assert store.apply_authoritative_health(
        snapshot,
        received_monotonic=10.0,
        received_utc=stamp + timedelta(seconds=11.0),
    )

    current = store.snapshot(
        now_monotonic=10.9,
        now_utc=stamp + timedelta(seconds=11.9),
    )
    expired = store.snapshot(
        now_monotonic=11.1,
        now_utc=stamp + timedelta(seconds=12.1),
    )

    assert current.health_evaluator_available is True
    assert expired.health_evaluator_available is False
    assert expired.fault == "authoritative health snapshot unavailable"


def test_future_snapshot_does_not_extend_the_transport_timeout():
    timeout_s = 12.0
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    store = GatewayStore(
        contract(),
        started_monotonic=0.0,
        authoritative_health_timeout_s=timeout_s,
    )
    snapshot = authoritative_snapshot(snapshot_time=stamp)
    assert store.apply_authoritative_health(
        snapshot,
        received_monotonic=10.0,
        received_utc=stamp - timedelta(seconds=11.0),
    )

    current = store.snapshot(
        now_monotonic=21.9,
        now_utc=stamp + timedelta(seconds=0.9),
    )
    expired = store.snapshot(
        now_monotonic=22.1,
        now_utc=stamp + timedelta(seconds=1.1),
    )

    assert current.health_evaluator_available is True
    assert expired.health_evaluator_available is False
