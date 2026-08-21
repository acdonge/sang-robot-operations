import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import time

import pytest
from asyncua import ua

from fleet_control_plane.opcua_shadow import (
    ShadowCache,
    ShadowContractError,
    ShadowValue,
    _phase2_defaults,
    apply_registered_opcua_profile,
    cache_is_aligned,
    compare_heartbeat,
    heartbeat_fields,
    parse_args,
    run_shadow,
)
from fleet_control_plane.operations_models import (
    AgentRegistrationCreate,
    ConnectionProfileCreate,
)
from fleet_control_plane.store import FleetStore
from robot_agent.local_health import (
    build_local_health_snapshot,
    parse_local_health_snapshot,
)
from robot_agent.monitor import RobotMonitor
from sang_opcua_gateway.contract import load_gateway_contract
from sang_opcua_gateway.server import OpcUaServerThread
from sang_opcua_gateway.state import GatewayStore


UTC = timezone.utc
STAMP = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def gateway_contract(endpoint):
    return load_gateway_contract(
        manifest_path=ROOT / "manifests/bi_so101_01.yaml",
        mapping_path=ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        model_contract_path=ROOT / "contracts/opcua/model-contract.yaml",
        status_codes_path=ROOT / "contracts/opcua/status-code-mapping.yaml",
        nodeset_path=ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        endpoint=endpoint,
        agent_id="workcell-soarm-01",
    )


def authoritative_heartbeat(gateway_contract, store):
    monitor = RobotMonitor(gateway_contract.manifest)
    robot = monitor.robot_snapshot(now_monotonic=time.monotonic())
    sent_at = datetime.now(UTC).isoformat()
    wire = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id="12345678-1234-5678-9234-567812345678",
        sequence=1,
        snapshot_time_utc=sent_at,
        manifest=gateway_contract.manifest,
        robot=robot,
        mapping_digest=gateway_contract.mapping_digest,
    )
    parsed = parse_local_health_snapshot(
        wire,
        expected_agent_id="workcell-soarm-01",
        manifest=gateway_contract.manifest,
        expected_mapping_digest=gateway_contract.mapping_digest,
    )
    store.apply_authoritative_health(parsed)
    return {
        "agent_id": "workcell-soarm-01",
        "status": "online",
        "age_s": 0.0,
        "sent_at": sent_at,
        "received_at": sent_at,
        "robots": [robot],
    }


def heartbeat():
    return {
        "agent_id": "workcell-soarm-01",
        "status": "online",
        "age_s": 0.0,
        "sent_at": STAMP.isoformat(),
        "robots": [
            {
                "robot_id": "bi_so101_01",
                "fsm_state": "READY",
                "phase": "MONITORING",
                "sources_fresh": True,
                "rates_ok": True,
                "fault": None,
                "sources": [
                    {
                        "key": "observation.state",
                        "fresh": True,
                        "rate_ok": True,
                        "topics": [
                            {
                                "topic": "/left",
                                "fresh": True,
                                "rate_ok": True,
                            },
                            {
                                "topic": "/right",
                                "fresh": True,
                                "rate_ok": True,
                            },
                        ],
                    },
                    {
                        "key": "action",
                        "fresh": False,
                        "rate_ok": None,
                        "topics": [
                            {
                                "topic": "/leader",
                                "fresh": False,
                                "rate_ok": None,
                            }
                        ],
                    },
                ],
            }
        ],
    }


def matching_cache(payload):
    cache = ShadowCache()
    for node_id, value in heartbeat_fields(
        payload,
        agent_id="workcell-soarm-01",
        robot_id="bi_so101_01",
    ).items():
        status = (
            ua.StatusCodes.BadWaitingForInitialData
            if value is None
            else ua.StatusCodes.Good
        )
        cache.values[node_id] = ShadowValue(
            value=False if value is None else value,
            status_code=status,
            source_timestamp=STAMP,
            server_timestamp=STAMP,
            central_received_at=STAMP,
        )
    return cache


def test_heartbeat_fields_cover_robot_source_and_topic_health():
    fields = heartbeat_fields(
        heartbeat(),
        agent_id="workcell-soarm-01",
        robot_id="bi_so101_01",
    )

    assert len(fields) == 15
    assert fields[
        "agent/workcell-soarm-01/robot/bi_so101_01/field/fault"
    ] == ""
    assert fields[
        "agent/workcell-soarm-01/robot/bi_so101_01/source/action/field/rate_ok"
    ] is None


def test_compare_heartbeat_matches_good_and_bad_waiting_values():
    payload = heartbeat()
    result = compare_heartbeat(
        payload,
        matching_cache(payload),
        agent_id="workcell-soarm-01",
        robot_id="bi_so101_01",
        comparison_window_s=6.0,
    )

    assert result.compared == 15
    assert result.matched == 15
    assert result.missing == ()
    assert result.timing_misses == ()
    assert result.divergences == ()


def test_compare_heartbeat_rejects_wrong_bad_status_for_null_health():
    payload = heartbeat()
    cache = matching_cache(payload)
    null_node = next(
        node_id
        for node_id, value in heartbeat_fields(
            payload,
            agent_id="workcell-soarm-01",
            robot_id="bi_so101_01",
        ).items()
        if value is None
    )
    previous = cache.values[null_node]
    cache.values[null_node] = ShadowValue(
        value=previous.value,
        status_code=ua.StatusCodes.BadNoCommunication,
        source_timestamp=previous.source_timestamp,
        server_timestamp=previous.server_timestamp,
        central_received_at=previous.central_received_at,
    )

    result = compare_heartbeat(
        payload,
        cache,
        agent_id="workcell-soarm-01",
        robot_id="bi_so101_01",
        comparison_window_s=6.0,
    )

    assert result.matched == 14
    assert result.divergences[0]["node_id"] == null_node
    assert result.divergences[0]["expected_status_code"] == (
        f"0x{ua.StatusCodes.BadWaitingForInitialData:08X}"
    )


def test_snapshot_alignment_requires_one_authoritative_source_timestamp():
    payload = heartbeat()
    cache = matching_cache(payload)
    assert cache_is_aligned(cache, cache.values, STAMP)

    node_id = next(iter(cache.values))
    previous = cache.values[node_id]
    cache.values[node_id] = ShadowValue(
        value=previous.value,
        status_code=previous.status_code,
        source_timestamp=STAMP + timedelta(milliseconds=2),
        server_timestamp=STAMP,
        central_received_at=STAMP,
    )
    assert not cache_is_aligned(cache, cache.values, STAMP)


def test_compare_heartbeat_reports_value_missing_and_timing_failures():
    payload = heartbeat()
    cache = matching_cache(payload)
    identifiers = list(cache.values)
    divergent = identifiers[0]
    missing = identifiers[1]
    late = identifiers[2]
    cache.values[divergent] = ShadowValue(
        value="DEGRADED",
        status_code=ua.StatusCodes.Good,
        source_timestamp=STAMP,
        server_timestamp=STAMP,
        central_received_at=STAMP,
    )
    del cache.values[missing]
    previous = cache.values[late]
    cache.values[late] = ShadowValue(
        value=previous.value,
        status_code=previous.status_code,
        source_timestamp=STAMP - timedelta(seconds=7),
        server_timestamp=STAMP,
        central_received_at=STAMP,
    )

    result = compare_heartbeat(
        payload,
        cache,
        agent_id="workcell-soarm-01",
        robot_id="bi_so101_01",
        comparison_window_s=6.0,
    )

    assert result.matched == 12
    assert result.missing == (missing,)
    assert result.timing_misses == (late,)
    assert result.divergences[0]["node_id"] == divergent


def test_phase2_defaults_are_loaded_from_the_frozen_contract():
    timing = _phase2_defaults(
        ROOT / "contracts/opcua/model-contract.yaml"
    )

    assert timing.comparison_window_s == 6.0
    assert timing.grace_s == 40.0
    assert timing.heartbeat_max_gap_s == 12.0
    assert timing.minimum_strict_duration_s == 7200.0


def test_cli_rejects_remote_endpoint(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--agent-id",
                "agent",
                "--robot-id",
                "robot",
                "--endpoint",
                "opc.tcp://192.168.0.5:4840/sang/",
                "--model-contract",
                str(ROOT / "contracts/opcua/model-contract.yaml"),
                "--report",
                str(tmp_path / "report.json"),
            ]
        )


def test_registered_profile_drives_shadow_endpoint_and_security_refs(tmp_path):
    db_path = tmp_path / "fleet.db"
    store = FleetStore(db_path)
    store.create_agent_registration(
        AgentRegistrationCreate(
            agent_id="workcell-soarm-01",
            display_name="SO-ARM Edge",
        ),
        occurred_at=STAMP,
    )
    profile = ConnectionProfileCreate(
        profile_id="soarm-opcua",
        owner_kind="agent",
        agent_id="workcell-soarm-01",
        protocol="opcua",
        desired_endpoint="opc.tcp://192.0.2.20:4840/sang/",
        expected_application_uri="urn:sang:agent:workcell-soarm-01",
        security_mode="SignAndEncrypt",
        security_policy="Basic256Sha256",
        client_certificate_ref="secret://opcua/application/cert.pem",
        client_private_key_ref="secret://opcua/application/key.pem",
        trust_list_ref="secret://opcua/trust/servers",
        crl_ref="secret://opcua/crl",
        user_certificate_ref="secret://opcua/user/cert.pem",
        user_private_key_ref="secret://opcua/user/key.pem",
    )
    store.create_connection_profile(profile, occurred_at=STAMP)
    common = [
        "--agent-id",
        "workcell-soarm-01",
        "--robot-id",
        "bi_so101_01",
        "--db-path",
        str(db_path),
        "--connection-profile-id",
        "soarm-opcua",
        "--security-mode",
        "sign-and-encrypt",
        "--secret-root",
        str(tmp_path),
        "--server-certificate-ref",
        "secret://opcua/pins/edge-cert.pem",
        "--model-contract",
        str(ROOT / "contracts/opcua/model-contract.yaml"),
        "--report",
        str(tmp_path / "report.json"),
    ]

    options = parse_args(common)
    _, loaded = apply_registered_opcua_profile(options)

    assert loaded["profile_id"] == "soarm-opcua"
    assert options.endpoint == profile.desired_endpoint
    assert options.application_certificate_ref == profile.client_certificate_ref
    assert options.server_trust_directory_ref == profile.trust_list_ref
    assert options.user_certificate_ref == profile.user_certificate_ref

    drifted = parse_args(
        [*common, "--endpoint", "opc.tcp://192.0.2.20:4841/sang/"]
    )
    with pytest.raises(ShadowContractError, match="differs"):
        apply_registered_opcua_profile(drifted)


def test_final_gate_enforces_frozen_duration_and_timing(tmp_path):
    common = [
        "--agent-id",
        "agent",
        "--robot-id",
        "robot",
        "--model-contract",
        str(ROOT / "contracts/opcua/model-contract.yaml"),
        "--report",
        str(tmp_path / "report.json"),
        "--final-gate",
    ]
    with pytest.raises(SystemExit):
        parse_args([*common, "--duration-s", "7199"])
    with pytest.raises(SystemExit):
        parse_args([*common, "--duration-s", "7200", "--grace-s", "0"])

    options = parse_args([*common, "--duration-s", "7200"])
    assert options.final_gate is True
    assert options.minimum_strict_duration_s == 7200.0
    assert options.heartbeat_max_gap_s == 12.0


def test_shadow_accepts_initial_bad_waiting_values_without_reconnect_loop():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return payload

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.8,
                poll_interval_s=0.05,
                comparison_window_s=6.0,
                grace_s=0.0,
                reconnect_backoff_s=0.05,
                heartbeat_max_gap_s=5.0,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is True
    assert report["strict_comparison_count"] > 0
    assert report["connection_epochs"] == 1
    assert report["divergent_field_count"] == 0
    assert report["central_snapshot_endpoint"] == "/api/v1/shadow/agents"


def test_shadow_does_not_fail_an_alignment_window_truncated_by_duration():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    advanced = {
        **payload,
        "sent_at": (
            datetime.fromisoformat(payload["sent_at"]) + timedelta(seconds=1)
        ).isoformat(),
    }
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
    started = 0.0

    async def fetch():
        if time.monotonic() - started < 0.25:
            return payload
        return advanced

    server.start()
    started = time.monotonic()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.4,
                poll_interval_s=0.03,
                comparison_window_s=0.2,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=2.0,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is True, report
    assert report["strict_comparison_count"] > 0
    assert report["alignment_timeout_count"] == 0


def test_final_gate_duration_starts_after_the_first_strict_match():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return payload

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.3,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.1,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=2.0,
                minimum_strict_duration_s=0.3,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is True
    assert report["final_gate"] is True
    assert report["strict_observation_s"] >= 0.29
    assert report["elapsed_s"] > report["strict_observation_s"]
    assert report["connection_epochs"] == 1


def test_final_gate_fails_immediately_on_opcua_disconnect():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return payload

    async def interrupt_final_gate():
        shadow = asyncio.create_task(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.5,
                poll_interval_s=1.0,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=2.0,
                minimum_strict_duration_s=0.1,
            )
        )
        await asyncio.sleep(0.3)
        await asyncio.to_thread(server.stop)
        return await shadow

    server.start()
    try:
        report = asyncio.run(interrupt_final_gate())
    finally:
        server.stop()

    assert report["passed"] is False
    assert report["disconnect_count"] == 1
    assert report["recovery_count"] == 0
    assert any("uninterrupted final gate" in failure for failure in report["failures"])


def test_final_gate_fails_on_the_first_post_grace_mismatch():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    robot = payload["robots"][0]
    mismatched = {
        **payload,
        "robots": [
            {
                **robot,
                "fsm_state": (
                    "READY" if robot["fsm_state"] != "READY" else "DEGRADED"
                ),
            }
        ],
    }
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return mismatched

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.5,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=2.0,
                minimum_strict_duration_s=0.1,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is False
    assert report["strict_observation_s"] == 0.0
    assert report["divergent_field_count"] == 1
    assert any("strict comparison did not match" in failure for failure in report["failures"])


def test_shadow_recovers_subscription_after_edge_restart():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    first_server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
    second_server = None

    async def fetch():
        return payload

    async def exercise_restart():
        nonlocal second_server
        shadow = asyncio.create_task(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=3.0,
                poll_interval_s=0.05,
                comparison_window_s=6.0,
                grace_s=0.0,
                reconnect_backoff_s=0.05,
                heartbeat_max_gap_s=5.0,
            )
        )
        await asyncio.sleep(0.45)
        await asyncio.to_thread(first_server.stop)
        await asyncio.sleep(0.2)
        second_server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
        await asyncio.to_thread(second_server.start)
        return await shadow

    first_server.start()
    try:
        report = asyncio.run(exercise_restart())
    finally:
        first_server.stop()
        if second_server is not None:
            second_server.stop()

    assert report["passed"] is True
    assert report["connection_epochs"] >= 2
    assert report["disconnect_count"] >= 1
    assert report["recovery_count"] >= 1
    assert all(report["strict_comparisons_by_epoch"])
    assert report["divergent_field_count"] == 0


def test_reconnect_is_not_recovered_until_the_new_epoch_compares_strictly():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    first_server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
    second_server = None

    async def fetch():
        return payload

    async def exercise_late_restart():
        nonlocal second_server
        shadow = asyncio.create_task(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=4.0,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=5.0,
                reconnect_grace_s=10.0,
            )
        )
        await asyncio.sleep(0.45)
        await asyncio.to_thread(first_server.stop)
        await asyncio.sleep(0.2)
        second_server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
        await asyncio.to_thread(second_server.start)
        return await shadow

    first_server.start()
    try:
        report = asyncio.run(exercise_late_restart())
    finally:
        first_server.stop()
        if second_server is not None:
            second_server.stop()

    assert report["passed"] is False
    assert report["connection_epochs"] >= 2, report
    assert report["strict_comparisons_by_epoch"][0] > 0
    assert report["strict_comparisons_by_epoch"][-1] == 0
    assert report["recovery_count"] == 0
    assert any("epoch(s)" in failure for failure in report["failures"])


def test_shadow_fails_when_agent_heartbeat_stops_advancing():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return payload

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.6,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=0.2,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is False
    assert any("did not advance" in failure for failure in report["failures"])


def test_shadow_rejects_a_late_heartbeat_even_when_sent_at_advances():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    advanced = {
        **payload,
        "sent_at": (
            datetime.fromisoformat(payload["sent_at"]) + timedelta(seconds=1)
        ).isoformat(),
    }
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            return payload
        await asyncio.sleep(0.2)
        return advanced

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.6,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=0.1,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is False
    assert any("update arrived after" in failure for failure in report["failures"])


def test_shadow_fails_when_agent_becomes_stale():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)
    started = time.monotonic()

    async def fetch():
        if time.monotonic() - started < 0.2:
            return payload
        return {**payload, "status": "stale", "age_s": 13.0}

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.6,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=12.0,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is False
    assert any("not 'online'" in failure for failure in report["failures"])


def test_shadow_fails_when_authority_snapshot_never_aligns():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    contract = gateway_contract(endpoint)
    store = GatewayStore(
        contract,
        started_monotonic=time.monotonic(),
        authoritative_health_timeout_s=12.0,
    )
    payload = authoritative_heartbeat(contract, store)
    payload = {
        **payload,
        "sent_at": (
            datetime.fromisoformat(payload["sent_at"]) + timedelta(seconds=1)
        ).isoformat(),
    }
    server = OpcUaServerThread(contract, store, publish_interval_s=0.02)

    async def fetch():
        return payload

    server.start()
    try:
        report = asyncio.run(
            run_shadow(
                endpoint=endpoint,
                agent_id="workcell-soarm-01",
                robot_id="bi_so101_01",
                fetch_heartbeat=fetch,
                duration_s=0.4,
                poll_interval_s=0.03,
                comparison_window_s=0.1,
                grace_s=0.0,
                reconnect_backoff_s=0.03,
                heartbeat_max_gap_s=5.0,
            )
        )
    finally:
        server.stop()

    assert report["passed"] is False
    assert report["alignment_timeout_count"] >= 1
