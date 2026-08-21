"""Read-only OPC UA shadow comparison against Central heartbeat snapshots."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import urlopen

from asyncua import Client, ua
import yaml

from sang_security.opcua import (
    OpcUaClientSecurity,
    OpcUaSecurityError,
    configure_secure_client,
)
from sang_security.certificates import certificate_sha256
from sang_security.refs import SecretReferenceError, SecretResolver
from fleet_control_plane.store import FleetStore
from fleet_control_plane.status import parse_datetime
from robot_agent.heartbeat import require_loopback_http
from sang_opcua_gateway.contract import (
    NodeIds,
    require_loopback_endpoint,
    require_secure_endpoint,
)


UTC = timezone.utc
ROBOT_FIELDS = (
    "fsm_state",
    "phase",
    "sources_fresh",
    "rates_ok",
    "fault",
)
SOURCE_FIELDS = ("fresh", "rate_ok")
TOPIC_FIELDS = ("fresh", "rate_ok")
MAX_DIVERGENCE_RECORDS = 200
ALIGNMENT_TOLERANCE_S = 0.001


class ShadowContractError(ValueError):
    """Raised when the heartbeat no longer matches the subscribed contract."""


@dataclass(frozen=True)
class ShadowValue:
    value: object
    status_code: int
    source_timestamp: datetime | None
    server_timestamp: datetime | None
    central_received_at: datetime


@dataclass(frozen=True)
class ComparisonResult:
    compared: int
    matched: int
    missing: tuple[str, ...]
    timing_misses: tuple[str, ...]
    divergences: tuple[dict, ...]


@dataclass(frozen=True)
class Phase2Timing:
    comparison_window_s: float
    grace_s: float
    heartbeat_max_gap_s: float
    minimum_strict_duration_s: float


class ShadowCache:
    def __init__(self) -> None:
        self.values: dict[str, ShadowValue] = {}

    def update(self, node_id: str, data_value: ua.DataValue) -> None:
        self.values[node_id] = ShadowValue(
            value=data_value.Value.Value,
            status_code=data_value.StatusCode.value,
            source_timestamp=data_value.SourceTimestamp,
            server_timestamp=data_value.ServerTimestamp,
            central_received_at=datetime.now(UTC),
        )


class _SubscriptionHandler:
    def __init__(self, cache: ShadowCache) -> None:
        self.cache = cache

    def datachange_notification(self, node, value, data) -> None:
        del value
        self.cache.update(str(node.nodeid.Identifier), data.monitored_item.Value)

    def status_change_notification(self, status) -> None:
        del status


def _robot(heartbeat: Mapping, robot_id: str) -> Mapping:
    for robot in heartbeat.get("robots", []):
        if robot.get("robot_id") == robot_id:
            return robot
    raise ShadowContractError(f"heartbeat does not contain robot {robot_id!r}")


def heartbeat_fields(
    heartbeat: Mapping,
    *,
    agent_id: str,
    robot_id: str,
) -> dict[str, object]:
    if heartbeat.get("agent_id") != agent_id:
        raise ShadowContractError("heartbeat agent_id differs from shadow identity")
    robot = _robot(heartbeat, robot_id)
    ids = NodeIds(agent_id, robot_id)
    fields = {
        ids.robot_field("fsm_state"): robot["fsm_state"],
        ids.robot_field("phase"): robot["phase"],
        ids.robot_field("sources_fresh"): robot["sources_fresh"],
        ids.robot_field("rates_ok"): robot["rates_ok"],
        ids.robot_field("fault"): robot.get("fault") or "",
    }
    for source in robot.get("sources", []):
        source_key = source["key"]
        for field in SOURCE_FIELDS:
            fields[ids.source_field(source_key, field)] = source.get(field)
        for topic in source.get("topics", []):
            topic_name = topic["topic"]
            for field in TOPIC_FIELDS:
                fields[ids.topic_field(source_key, topic_name, field)] = topic.get(field)
    return fields


def validate_heartbeat_liveness(
    heartbeat: Mapping,
    *,
    maximum_age_s: float,
) -> tuple[datetime, float]:
    if heartbeat.get("status") != "online":
        raise ShadowContractError(
            f"Central Agent status is {heartbeat.get('status')!r}, not 'online'"
        )
    age_s = heartbeat.get("age_s")
    if (
        not isinstance(age_s, (int, float))
        or isinstance(age_s, bool)
        or not math.isfinite(age_s)
        or age_s < 0.0
    ):
        raise ShadowContractError("Central Agent age_s is invalid")
    if age_s > maximum_age_s:
        raise ShadowContractError(
            f"Central Agent heartbeat age {age_s:.3f}s exceeds {maximum_age_s:.3f}s"
        )
    try:
        sent_at = parse_datetime(heartbeat["sent_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShadowContractError("Central Agent sent_at is invalid") from exc
    return sent_at, float(age_s)


def _normalized_actual(sample: ShadowValue) -> object:
    status = ua.StatusCode(sample.status_code)
    return sample.value if status.is_good() else None


def compare_heartbeat(
    heartbeat: Mapping,
    cache: ShadowCache,
    *,
    agent_id: str,
    robot_id: str,
    comparison_window_s: float,
) -> ComparisonResult:
    expected = heartbeat_fields(
        heartbeat,
        agent_id=agent_id,
        robot_id=robot_id,
    )
    heartbeat_time = parse_datetime(heartbeat["sent_at"])
    missing = []
    timing_misses = []
    divergences = []
    compared = 0
    matched = 0
    for node_id, expected_value in expected.items():
        sample = cache.values.get(node_id)
        if sample is None:
            missing.append(node_id)
            continue
        if sample.source_timestamp is None or abs(
            (sample.source_timestamp - heartbeat_time).total_seconds()
        ) > comparison_window_s:
            timing_misses.append(node_id)
            continue
        compared += 1
        actual = _normalized_actual(sample)
        expected_status = (
            ua.StatusCodes.Good
            if expected_value is not None
            else ua.StatusCodes.BadWaitingForInitialData
        )
        if actual == expected_value and sample.status_code == expected_status:
            matched += 1
            continue
        divergences.append(
            {
                "node_id": node_id,
                "heartbeat_value": expected_value,
                "opcua_value": actual,
                "expected_status_code": f"0x{expected_status:08X}",
                "status_code": f"0x{sample.status_code:08X}",
                "source_timestamp": sample.source_timestamp.isoformat(),
                "central_received_at": sample.central_received_at.isoformat(),
            }
        )
    return ComparisonResult(
        compared=compared,
        matched=matched,
        missing=tuple(missing),
        timing_misses=tuple(timing_misses),
        divergences=tuple(divergences),
    )


def cache_is_aligned(
    cache: ShadowCache,
    node_ids,
    snapshot_time: datetime,
    *,
    tolerance_s: float = ALIGNMENT_TOLERANCE_S,
) -> bool:
    for node_id in node_ids:
        sample = cache.values.get(node_id)
        if sample is None or sample.source_timestamp is None:
            return False
        delta = abs((sample.source_timestamp - snapshot_time).total_seconds())
        if delta > tolerance_s:
            return False
    return True


def comparison_is_exact(result: ComparisonResult, expected_count: int) -> bool:
    return (
        result.compared == expected_count
        and result.matched == expected_count
        and not result.missing
        and not result.timing_misses
        and not result.divergences
    )


def fetch_agent_http(
    control_plane_url: str,
    agent_id: str,
    *,
    timeout_s: float,
) -> dict:
    require_loopback_http(control_plane_url)
    with urlopen(
        control_plane_url.rstrip("/") + "/api/v1/shadow/agents",
        timeout=timeout_s,
    ) as response:
        agents = json.loads(response.read())
    for agent in agents:
        if agent.get("agent_id") == agent_id:
            return agent
    raise ShadowContractError(f"Central does not contain Agent {agent_id!r}")


async def _sleep_bounded(delay_s: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0.0:
        await asyncio.sleep(min(delay_s, remaining))


async def run_shadow(
    *,
    endpoint: str,
    agent_id: str,
    robot_id: str,
    fetch_heartbeat: Callable[[], Awaitable[dict]],
    duration_s: float,
    poll_interval_s: float,
    comparison_window_s: float,
    grace_s: float,
    reconnect_backoff_s: float,
    heartbeat_max_gap_s: float,
    reconnect_grace_s: float | None = None,
    minimum_strict_duration_s: float = 0.0,
    client_security: OpcUaClientSecurity | None = None,
) -> dict:
    if client_security is None:
        require_loopback_endpoint(endpoint)
    else:
        require_secure_endpoint(endpoint)
    if not math.isfinite(heartbeat_max_gap_s) or heartbeat_max_gap_s <= 0.0:
        raise ValueError("heartbeat_max_gap_s must be positive")
    if (
        not math.isfinite(minimum_strict_duration_s)
        or minimum_strict_duration_s < 0.0
    ):
        raise ValueError("minimum_strict_duration_s must be non-negative")
    if reconnect_grace_s is None:
        reconnect_grace_s = grace_s
    if not math.isfinite(reconnect_grace_s) or reconnect_grace_s < 0.0:
        raise ValueError("reconnect_grace_s must be non-negative")
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    final_gate = minimum_strict_duration_s > 0.0
    deadline = started_monotonic + (
        grace_s + comparison_window_s + heartbeat_max_gap_s
        if final_gate
        else duration_s
    )
    connection_epochs = 0
    disconnect_count = 0
    recovery_count = 0
    strict_comparisons_by_epoch: list[int] = []
    comparison_count = 0
    strict_comparison_count = 0
    compared_fields = 0
    matched_fields = 0
    missing_fields = 0
    timing_miss_fields = 0
    divergent_fields = 0
    grace_ignored_divergences = 0
    alignment_wait_count = 0
    alignment_timeout_count = 0
    grace_ignored_alignment_timeout_count = 0
    divergence_records: list[dict] = []
    connection_events: list[dict] = []
    errors: list[str] = []
    ever_connected = False
    pending_recovery = False
    connected_at_finish = False
    last_seen_heartbeat_time: datetime | None = None
    last_heartbeat_change_monotonic: float | None = None
    maximum_heartbeat_age_s = 0.0
    maximum_heartbeat_gap_observed_s = 0.0
    strict_started_monotonic: float | None = None
    fatal_error = None
    observed_namespace_uris: list[str] = []

    def observe_heartbeat(heartbeat: Mapping) -> datetime:
        nonlocal last_seen_heartbeat_time
        nonlocal last_heartbeat_change_monotonic
        nonlocal maximum_heartbeat_age_s
        nonlocal maximum_heartbeat_gap_observed_s

        sent_at, age_s = validate_heartbeat_liveness(
            heartbeat,
            maximum_age_s=heartbeat_max_gap_s,
        )
        observed_at = time.monotonic()
        maximum_heartbeat_age_s = max(maximum_heartbeat_age_s, age_s)
        if last_seen_heartbeat_time is None:
            last_seen_heartbeat_time = sent_at
            last_heartbeat_change_monotonic = observed_at
            return sent_at
        if sent_at < last_seen_heartbeat_time:
            raise ShadowContractError("Central Agent heartbeat sent_at moved backwards")
        if sent_at > last_seen_heartbeat_time:
            gap_s = observed_at - last_heartbeat_change_monotonic
            maximum_heartbeat_gap_observed_s = max(
                maximum_heartbeat_gap_observed_s,
                gap_s,
            )
            if gap_s > heartbeat_max_gap_s:
                raise ShadowContractError(
                    "Central Agent heartbeat update arrived after the frozen "
                    f"{heartbeat_max_gap_s:.3f}s maximum gap"
                )
            last_seen_heartbeat_time = sent_at
            last_heartbeat_change_monotonic = observed_at
            return sent_at
        gap_s = observed_at - last_heartbeat_change_monotonic
        maximum_heartbeat_gap_observed_s = max(
            maximum_heartbeat_gap_observed_s,
            gap_s,
        )
        if gap_s > heartbeat_max_gap_s:
            raise ShadowContractError(
                "Central Agent heartbeat sent_at did not advance within "
                f"{heartbeat_max_gap_s:.3f}s"
            )
        return sent_at

    while time.monotonic() < deadline and fatal_error is None:
        connected_this_epoch = False
        subscription = None
        try:
            heartbeat = await fetch_heartbeat()
            observe_heartbeat(heartbeat)
            expected = heartbeat_fields(
                heartbeat,
                agent_id=agent_id,
                robot_id=robot_id,
            )
            client = Client(url=endpoint, timeout=5.0)
            if client_security is not None:
                await configure_secure_client(client, client_security)
            async with client:
                observed_namespace_uris = list(await client.get_namespace_array())
                edge_ns = await client.get_namespace_index("urn:sang:fleet:edge:1")
                cache = ShadowCache()
                nodes = [
                    client.get_node(ua.NodeId(identifier, edge_ns))
                    for identifier in expected
                ]
                node_classes = await client.read_attributes(
                    nodes,
                    ua.AttributeIds.NodeClass,
                )
                for identifier, data_value in zip(
                    expected,
                    node_classes,
                    strict=True,
                ):
                    data_value.StatusCode.check()
                    if data_value.Value.Value != ua.NodeClass.Variable:
                        raise ShadowContractError(
                            f"subscribed NodeId is not a Variable: {identifier}"
                        )
                subscription = await client.create_subscription(
                    1000.0,
                    _SubscriptionHandler(cache),
                )
                await subscription.subscribe_data_change(nodes, queuesize=1)
                initial = await client.read_attributes(nodes, ua.AttributeIds.Value)
                for node, data_value in zip(nodes, initial, strict=True):
                    cache.update(str(node.nodeid.Identifier), data_value)

                connection_epochs += 1
                strict_comparisons_by_epoch.append(0)
                connected_this_epoch = True
                connected_at_finish = True
                ever_connected = True
                epoch_grace_s = (
                    grace_s if connection_epochs == 1 else reconnect_grace_s
                )
                grace_until = time.monotonic() + epoch_grace_s
                last_compared_heartbeat_time: datetime | None = None
                connection_events.append(
                    {
                        "event": "connected" if connection_epochs == 1 else "reconnected",
                        "at": datetime.now(UTC).isoformat(),
                        "epoch": connection_epochs,
                    }
                )

                while time.monotonic() < deadline:
                    await client.nodes.server_state.read_value()
                    heartbeat = await fetch_heartbeat()
                    heartbeat_time = observe_heartbeat(heartbeat)
                    current_fields = heartbeat_fields(
                        heartbeat,
                        agent_id=agent_id,
                        robot_id=robot_id,
                    )
                    if current_fields.keys() != expected.keys():
                        raise ShadowContractError(
                            "heartbeat topology changed while the shadow was subscribed"
                        )
                    if heartbeat_time != last_compared_heartbeat_time:
                        alignment_deadline = min(
                            time.monotonic() + comparison_window_s,
                            deadline,
                        )
                        deadline_limited = alignment_deadline == deadline
                        aligned = False
                        while time.monotonic() < alignment_deadline:
                            current = await client.read_attributes(
                                nodes,
                                ua.AttributeIds.Value,
                            )
                            for node, data_value in zip(
                                nodes,
                                current,
                                strict=True,
                            ):
                                cache.update(
                                    str(node.nodeid.Identifier),
                                    data_value,
                                )
                            if cache_is_aligned(
                                cache,
                                expected,
                                heartbeat_time,
                            ):
                                aligned = True
                                break
                            alignment_wait_count += 1
                            await _sleep_bounded(0.05, alignment_deadline)
                            heartbeat = await fetch_heartbeat()
                            heartbeat_time = observe_heartbeat(heartbeat)
                            current_fields = heartbeat_fields(
                                heartbeat,
                                agent_id=agent_id,
                                robot_id=robot_id,
                            )
                            if current_fields.keys() != expected.keys():
                                raise ShadowContractError(
                                    "heartbeat topology changed while the shadow was subscribed"
                                )

                        in_grace = time.monotonic() < grace_until
                        if not aligned:
                            if (
                                deadline_limited
                                and not final_gate
                                and time.monotonic() >= deadline
                            ):
                                break
                            if in_grace:
                                grace_ignored_alignment_timeout_count += 1
                            else:
                                alignment_timeout_count += 1
                                last_compared_heartbeat_time = heartbeat_time
                                if final_gate:
                                    raise ShadowContractError(
                                        "final gate health snapshot did not align"
                                    )
                            continue
                        result = compare_heartbeat(
                            heartbeat,
                            cache,
                            agent_id=agent_id,
                            robot_id=robot_id,
                            comparison_window_s=comparison_window_s,
                        )
                        comparison_count += 1
                        if not in_grace:
                            last_compared_heartbeat_time = heartbeat_time
                            strict_comparison_count += 1
                            strict_comparisons_by_epoch[-1] += 1
                            compared_fields += result.compared
                            matched_fields += result.matched
                            missing_fields += len(result.missing)
                            timing_miss_fields += len(result.timing_misses)
                            divergent_fields += len(result.divergences)
                            remaining = MAX_DIVERGENCE_RECORDS - len(divergence_records)
                            if remaining > 0:
                                divergence_records.extend(result.divergences[:remaining])
                            strict_match = comparison_is_exact(result, len(expected))
                            if final_gate and not strict_match:
                                raise ShadowContractError(
                                    "final gate strict comparison did not match"
                                )
                            if strict_match and strict_started_monotonic is None:
                                strict_started_monotonic = time.monotonic()
                                if final_gate:
                                    deadline = strict_started_monotonic + duration_s
                                connection_events.append(
                                    {
                                        "event": "strict_window_started",
                                        "at": datetime.now(UTC).isoformat(),
                                        "epoch": connection_epochs,
                                    }
                                )
                            if pending_recovery and strict_match:
                                recovery_count += 1
                                pending_recovery = False
                                connection_events.append(
                                    {
                                        "event": "recovered",
                                        "at": datetime.now(UTC).isoformat(),
                                        "epoch": connection_epochs,
                                    }
                                )
                        else:
                            grace_ignored_divergences += len(result.divergences)
                    await _sleep_bounded(poll_interval_s, deadline)
                if final_gate and strict_started_monotonic is not None:
                    await client.nodes.server_state.read_value()
                    heartbeat = await fetch_heartbeat()
                    heartbeat_time = observe_heartbeat(heartbeat)
                    current_fields = heartbeat_fields(
                        heartbeat,
                        agent_id=agent_id,
                        robot_id=robot_id,
                    )
                    if current_fields.keys() != expected.keys():
                        raise ShadowContractError(
                            "heartbeat topology changed before the final probe"
                        )
                    final_probe_deadline = time.monotonic() + comparison_window_s
                    aligned = False
                    while time.monotonic() < final_probe_deadline:
                        current = await client.read_attributes(
                            nodes,
                            ua.AttributeIds.Value,
                        )
                        for node, data_value in zip(nodes, current, strict=True):
                            cache.update(str(node.nodeid.Identifier), data_value)
                        if cache_is_aligned(cache, expected, heartbeat_time):
                            aligned = True
                            break
                        alignment_wait_count += 1
                        await asyncio.sleep(
                            max(
                                0.0,
                                min(
                                    0.05,
                                    final_probe_deadline - time.monotonic(),
                                ),
                            )
                        )
                        heartbeat = await fetch_heartbeat()
                        heartbeat_time = observe_heartbeat(heartbeat)
                        current_fields = heartbeat_fields(
                            heartbeat,
                            agent_id=agent_id,
                            robot_id=robot_id,
                        )
                        if current_fields.keys() != expected.keys():
                            raise ShadowContractError(
                                "heartbeat topology changed before the final probe"
                            )
                    if not aligned:
                        alignment_timeout_count += 1
                        raise ShadowContractError(
                            "final gate probe did not align before timeout"
                        )
                    result = compare_heartbeat(
                        heartbeat,
                        cache,
                        agent_id=agent_id,
                        robot_id=robot_id,
                        comparison_window_s=comparison_window_s,
                    )
                    comparison_count += 1
                    strict_comparison_count += 1
                    strict_comparisons_by_epoch[-1] += 1
                    compared_fields += result.compared
                    matched_fields += result.matched
                    missing_fields += len(result.missing)
                    timing_miss_fields += len(result.timing_misses)
                    divergent_fields += len(result.divergences)
                    remaining = MAX_DIVERGENCE_RECORDS - len(divergence_records)
                    if remaining > 0:
                        divergence_records.extend(result.divergences[:remaining])
                    if not comparison_is_exact(result, len(expected)):
                        raise ShadowContractError(
                            "final gate probe strict comparison did not match"
                        )
                    connection_events.append(
                        {
                            "event": "final_probe_passed",
                            "at": datetime.now(UTC).isoformat(),
                            "epoch": connection_epochs,
                        }
                    )
                connected_at_finish = True
        except ShadowContractError as exc:
            fatal_error = str(exc)
        except Exception as exc:
            connected_at_finish = False
            if connected_this_epoch:
                disconnect_count += 1
                pending_recovery = True
                connection_events.append(
                    {
                        "event": "disconnected",
                        "at": datetime.now(UTC).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                        "epoch": connection_epochs,
                    }
                )
            if len(errors) < 20:
                errors.append(f"{type(exc).__name__}: {exc}")
            if final_gate:
                fatal_error = (
                    "OPC UA disconnected during the uninterrupted final gate: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                await _sleep_bounded(reconnect_backoff_s, deadline)
        finally:
            if subscription is not None:
                with suppress(Exception):
                    await subscription.delete()

    elapsed_s = time.monotonic() - started_monotonic
    strict_observation_s = (
        0.0
        if strict_started_monotonic is None
        else time.monotonic() - strict_started_monotonic
    )
    if last_heartbeat_change_monotonic is not None:
        maximum_heartbeat_gap_observed_s = max(
            maximum_heartbeat_gap_observed_s,
            time.monotonic() - last_heartbeat_change_monotonic,
        )
    failures = []
    if fatal_error is not None:
        failures.append(fatal_error)
    if not ever_connected:
        failures.append("OPC UA shadow never connected")
    if not connected_at_finish:
        failures.append("OPC UA shadow ended disconnected")
    if strict_comparison_count == 0:
        failures.append("no post-grace heartbeat comparison completed")
    epochs_without_strict = [
        index
        for index, count in enumerate(strict_comparisons_by_epoch, start=1)
        if count == 0
    ]
    if epochs_without_strict:
        failures.append(
            "no post-grace heartbeat comparison completed in OPC UA epoch(s): "
            + ", ".join(str(epoch) for epoch in epochs_without_strict)
        )
    if missing_fields:
        failures.append(f"{missing_fields} subscribed field(s) were missing")
    if timing_miss_fields:
        failures.append(
            f"{timing_miss_fields} field(s) exceeded the timestamp comparison window"
        )
    if alignment_timeout_count:
        failures.append(
            f"{alignment_timeout_count} health snapshot alignment timeout(s)"
        )
    if divergent_fields:
        failures.append(f"{divergent_fields} heartbeat/OPC UA field divergence(s)")
    if pending_recovery:
        failures.append("the final OPC UA disconnect did not recover")
    if maximum_heartbeat_gap_observed_s > heartbeat_max_gap_s:
        failures.append(
            f"maximum heartbeat gap {maximum_heartbeat_gap_observed_s:.3f}s "
            f"exceeded {heartbeat_max_gap_s:.3f}s"
        )
    if final_gate:
        if duration_s < minimum_strict_duration_s:
            failures.append(
                f"requested strict duration {duration_s:.3f}s is below the frozen "
                f"minimum {minimum_strict_duration_s:.3f}s"
            )
        if strict_started_monotonic is None:
            failures.append("the uninterrupted strict observation window never started")
        elif strict_observation_s + 0.05 < duration_s:
            failures.append(
                f"strict observation duration {strict_observation_s:.3f}s is below "
                f"the requested {duration_s:.3f}s"
            )
        if disconnect_count:
            failures.append("the final gate requires one uninterrupted OPC UA epoch")

    return {
        "schema_version": 1,
        "mode": "opcua_phase2_central_shadow",
        "passed": not failures,
        "agent_id": agent_id,
        "robot_id": robot_id,
        "endpoint": endpoint,
        "security_mode": (
            "None" if client_security is None else "SignAndEncrypt"
        ),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "requested_duration_s": duration_s,
        "elapsed_s": round(elapsed_s, 3),
        "comparison_window_s": comparison_window_s,
        "grace_s": grace_s,
        "startup_grace_s": grace_s,
        "reconnect_grace_s": reconnect_grace_s,
        "heartbeat_max_gap_s": heartbeat_max_gap_s,
        "maximum_heartbeat_age_s": round(maximum_heartbeat_age_s, 3),
        "maximum_heartbeat_gap_observed_s": round(
            maximum_heartbeat_gap_observed_s,
            3,
        ),
        "final_gate": final_gate,
        "requested_strict_duration_s": duration_s if final_gate else 0.0,
        "minimum_strict_duration_s": minimum_strict_duration_s,
        "strict_observation_s": round(strict_observation_s, 3),
        "connection_epochs": connection_epochs,
        "disconnect_count": disconnect_count,
        "recovery_count": recovery_count,
        "comparison_count": comparison_count,
        "strict_comparison_count": strict_comparison_count,
        "strict_comparisons_by_epoch": strict_comparisons_by_epoch,
        "compared_field_count": compared_fields,
        "matched_field_count": matched_fields,
        "missing_field_count": missing_fields,
        "timing_miss_field_count": timing_miss_fields,
        "divergent_field_count": divergent_fields,
        "grace_ignored_divergence_count": grace_ignored_divergences,
        "alignment_wait_count": alignment_wait_count,
        "alignment_timeout_count": alignment_timeout_count,
        "grace_ignored_alignment_timeout_count": (
            grace_ignored_alignment_timeout_count
        ),
        "divergences": divergence_records,
        "connection_events": connection_events,
        "connection_errors": errors,
        "connected_at_finish": connected_at_finish,
        "namespace_uris": observed_namespace_uris,
        "central_snapshot_endpoint": "/api/v1/shadow/agents",
        "failures": failures,
    }


def _phase2_defaults(path: Path) -> Phase2Timing:
    with path.open("r", encoding="utf-8") as stream:
        contract = yaml.safe_load(stream)
    try:
        phase2 = contract["timestamps"]["phase_2"]
        if phase2["status"] != "frozen":
            raise ValueError("Phase 2 timing is not frozen")
        startup_grace_s = float(phase2["startup_grace_s"])
        reconnect_grace_s = float(phase2["reconnect_grace_s"])
        if startup_grace_s != reconnect_grace_s:
            raise ValueError("startup and reconnect grace must match")
        timing = Phase2Timing(
            comparison_window_s=float(phase2["comparison_window_s"]),
            grace_s=startup_grace_s,
            heartbeat_max_gap_s=float(phase2["heartbeat_max_gap_s"]),
            minimum_strict_duration_s=float(
                phase2["minimum_strict_duration_s"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ShadowContractError("model contract is missing Phase 2 timing") from exc
    values = (
        timing.comparison_window_s,
        timing.grace_s,
        timing.heartbeat_max_gap_s,
        timing.minimum_strict_duration_s,
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise ShadowContractError("model contract Phase 2 timing must be positive")
    return timing


def apply_registered_opcua_profile(
    options: argparse.Namespace,
) -> tuple[FleetStore, dict]:
    """Load the enabled Central profile and reject duplicate CLI drift."""
    store = FleetStore(options.db_path)
    profile = store.get_agent_connection_profile(options.agent_id, "opcua")
    if profile is None:
        raise ShadowContractError("Agent has no registered OPC UA connection profile")
    if profile["profile_id"] != options.connection_profile_id:
        raise ShadowContractError(
            "registered OPC UA profile ID differs from --connection-profile-id"
        )
    if not profile["enabled"]:
        raise ShadowContractError("registered OPC UA connection profile is disabled")
    if (
        profile["security_mode"] != "SignAndEncrypt"
        or profile["security_policy"] != "Basic256Sha256"
    ):
        raise ShadowContractError(
            "registered OPC UA profile is not Basic256Sha256 SignAndEncrypt"
        )
    expected_uri = f"urn:sang:agent:{options.agent_id}"
    if profile["expected_application_uri"] != expected_uri:
        raise ShadowContractError(
            "registered OPC UA ApplicationUri differs from the Agent identity"
        )

    profile_values = {
        "endpoint": profile["desired_endpoint"],
        "application_certificate_ref": profile["client_certificate_ref"],
        "application_private_key_ref": profile["client_private_key_ref"],
        "server_trust_directory_ref": profile["trust_list_ref"],
        "server_crl_directory_ref": profile["crl_ref"],
        "user_certificate_ref": profile["user_certificate_ref"],
        "user_private_key_ref": profile["user_private_key_ref"],
        "server_certificate_ref": profile["server_certificate_ref"],
    }
    for option_name, configured_value in profile_values.items():
        supplied_value = getattr(options, option_name)
        if supplied_value is not None and supplied_value != configured_value:
            raise ShadowContractError(
                f"--{option_name.replace('_', '-')} differs from the registered profile"
            )
        setattr(options, option_name, configured_value)
    require_secure_endpoint(options.endpoint)
    return store, profile


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare loopback OPC UA health against Central heartbeat snapshots"
    )
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:18091")
    parser.add_argument("--endpoint")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--connection-profile-id")
    parser.add_argument("--duration-s", type=float, default=7200.0)
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    parser.add_argument("--http-timeout-s", type=float, default=2.0)
    parser.add_argument("--reconnect-backoff-s", type=float, default=1.0)
    parser.add_argument(
        "--model-contract",
        type=Path,
        default=Path("/ws/contracts/opcua/model-contract.yaml"),
    )
    parser.add_argument("--comparison-window-s", type=float)
    parser.add_argument("--grace-s", type=float)
    parser.add_argument("--heartbeat-max-gap-s", type=float)
    parser.add_argument(
        "--final-gate",
        action="store_true",
        help="enforce all frozen Phase 2 timing and the two-hour minimum",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--security-mode",
        choices=("none", "sign-and-encrypt"),
        default="none",
    )
    parser.add_argument("--secret-root", type=Path)
    parser.add_argument("--application-uri", default="urn:sang:central:control-plane")
    parser.add_argument("--application-certificate-ref")
    parser.add_argument("--application-private-key-ref")
    parser.add_argument("--server-trust-directory-ref")
    parser.add_argument("--server-crl-directory-ref")
    parser.add_argument("--user-certificate-ref")
    parser.add_argument("--user-private-key-ref")
    parser.add_argument("--server-certificate-ref")
    options = parser.parse_args(args)
    profile_mode = options.connection_profile_id is not None
    if (options.db_path is None) != (options.connection_profile_id is None):
        parser.error("--db-path and --connection-profile-id must be supplied together")
    if options.endpoint is None and not profile_mode:
        options.endpoint = "opc.tcp://127.0.0.1:4840/sang/"
    try:
        require_loopback_http(options.control_plane_url)
        if options.endpoint is not None:
            if options.security_mode == "none":
                require_loopback_endpoint(options.endpoint)
            else:
                require_secure_endpoint(options.endpoint)
        timing = _phase2_defaults(options.model_contract)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    if options.final_gate:
        overrides = (
            ("comparison-window-s", options.comparison_window_s, timing.comparison_window_s),
            ("grace-s", options.grace_s, timing.grace_s),
            (
                "heartbeat-max-gap-s",
                options.heartbeat_max_gap_s,
                timing.heartbeat_max_gap_s,
            ),
        )
        for name, supplied, frozen in overrides:
            if supplied is not None and supplied != frozen:
                parser.error(f"--final-gate requires frozen --{name}={frozen:g}")
        if options.duration_s < timing.minimum_strict_duration_s:
            parser.error(
                "--final-gate requires duration-s >= "
                f"{timing.minimum_strict_duration_s:g}"
            )
    if options.comparison_window_s is None:
        options.comparison_window_s = timing.comparison_window_s
    if options.grace_s is None:
        options.grace_s = timing.grace_s
    if options.heartbeat_max_gap_s is None:
        options.heartbeat_max_gap_s = timing.heartbeat_max_gap_s
    options.minimum_strict_duration_s = timing.minimum_strict_duration_s
    positive = (
        "duration_s",
        "poll_interval_s",
        "http_timeout_s",
        "reconnect_backoff_s",
        "comparison_window_s",
        "heartbeat_max_gap_s",
    )
    if any(
        not math.isfinite(getattr(options, name)) or getattr(options, name) <= 0.0
        for name in positive
    ):
        parser.error("duration, intervals, timeout and comparison window must be positive")
    if not math.isfinite(options.grace_s) or options.grace_s < 0.0:
        parser.error("grace-s must be non-negative")
    security_values = (
        options.secret_root,
        options.application_certificate_ref,
        options.application_private_key_ref,
        options.server_trust_directory_ref,
        options.server_crl_directory_ref,
        options.user_certificate_ref,
        options.user_private_key_ref,
        options.server_certificate_ref,
    )
    if options.security_mode == "none" and any(value is not None for value in security_values):
        parser.error("security material requires --security-mode sign-and-encrypt")
    if profile_mode and options.security_mode != "sign-and-encrypt":
        parser.error("registered OPC UA profiles require sign-and-encrypt mode")
    if options.security_mode == "sign-and-encrypt":
        required = (
            (options.secret_root, options.server_certificate_ref)
            if profile_mode
            else security_values[:-1]
        )
        if any(value is None for value in required):
            if profile_mode:
                parser.error(
                    "registered OPC UA profiles require secret root and server pin"
                )
            parser.error(
                "sign-and-encrypt requires all security references except server pin"
            )
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    profile_store = None
    profile = None
    if options.connection_profile_id is not None:
        try:
            profile_store, profile = apply_registered_opcua_profile(options)
        except (OSError, ShadowContractError, ValueError) as exc:
            raise SystemExit(f"invalid registered OPC UA profile: {exc}") from exc
    client_security = None
    if options.security_mode == "sign-and-encrypt":
        try:
            client_security = OpcUaClientSecurity.from_secret_refs(
                SecretResolver(options.secret_root),
                application_uri=options.application_uri,
                expected_server_application_uri=(
                    profile["expected_application_uri"]
                    if profile is not None
                    else f"urn:sang:agent:{options.agent_id}"
                ),
                application_certificate_ref=options.application_certificate_ref,
                application_private_key_ref=options.application_private_key_ref,
                trust_directory_ref=options.server_trust_directory_ref,
                crl_directory_ref=options.server_crl_directory_ref,
                user_certificate_ref=options.user_certificate_ref,
                user_private_key_ref=options.user_private_key_ref,
                server_certificate_ref=options.server_certificate_ref,
            )
        except (SecretReferenceError, OpcUaSecurityError, OSError, ValueError) as exc:
            raise SystemExit(f"invalid OPC UA client security material: {exc}") from exc

    async def fetch() -> dict:
        return await asyncio.to_thread(
            fetch_agent_http,
            options.control_plane_url,
            options.agent_id,
            timeout_s=options.http_timeout_s,
        )

    report = asyncio.run(
        run_shadow(
            endpoint=options.endpoint,
            agent_id=options.agent_id,
            robot_id=options.robot_id,
            fetch_heartbeat=fetch,
            duration_s=options.duration_s,
            poll_interval_s=options.poll_interval_s,
            comparison_window_s=options.comparison_window_s,
            grace_s=options.grace_s,
            reconnect_backoff_s=options.reconnect_backoff_s,
            heartbeat_max_gap_s=options.heartbeat_max_gap_s,
            minimum_strict_duration_s=(
                options.minimum_strict_duration_s if options.final_gate else 0.0
            ),
            client_security=client_security,
        )
    )
    if profile_store is not None and profile is not None:
        parsed_endpoint = urlsplit(options.endpoint)
        failure = "; ".join(report["failures"]) or "OPC UA shadow validation failed"
        profile_store.record_connection_observation(
            profile["profile_id"],
            observed_at=datetime.now(UTC),
            observed_endpoint=options.endpoint,
            observed_application_uri=profile["expected_application_uri"],
            peer_certificate_sha256=(
                certificate_sha256(client_security.server_certificate)
                if client_security is not None
                and client_security.server_certificate is not None
                else None
            ),
            peer_address=f"{parsed_endpoint.hostname}:{parsed_endpoint.port}",
            namespace_uris=report["namespace_uris"],
            succeeded=report["passed"],
            error=None if report["passed"] else failure,
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
