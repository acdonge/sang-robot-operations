"""Thread-safe latest-value and health state for the OPC UA projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
import time
from typing import Mapping, Sequence

from robot_agent.health import (
    HealthSourceConfig,
    HealthTopicConfig,
    HealthTracker,
    camera_format_is_valid,
    joint_sample_is_valid,
)
from robot_agent.local_health import LocalHealthSnapshot
from sang_opcua_gateway.contract import GatewayContract, TopicSpec


UTC = timezone.utc


@dataclass(frozen=True)
class ValueSnapshot:
    value: object | None
    status: str
    source_timestamp: datetime | None
    provenance: str


@dataclass(frozen=True)
class TopicSnapshot:
    topic: str
    age_ms: float | None
    rate_hz: float | None
    minimum_rate_hz: float | None
    rate_ok: bool | None
    sequence: int
    fresh: bool
    sample: ValueSnapshot
    features: Mapping[str, ValueSnapshot]


@dataclass(frozen=True)
class SourceSnapshot:
    key: str
    kind: str
    required: bool
    freshness_ms: int
    fresh: bool
    rate_ok: bool | None
    topics: Mapping[str, TopicSnapshot]


@dataclass(frozen=True)
class RobotSnapshot:
    evaluated_at: datetime
    health_evaluator_available: bool
    fsm_state: str
    phase: str
    sources_fresh: bool
    rates_ok: bool
    fault: str
    sources: Mapping[str, SourceSnapshot]


@dataclass
class _TopicState:
    spec: TopicSpec
    source_timestamp: datetime | None = None
    provenance: str = ""
    feature_values: dict[str, float] | None = None
    has_sample: bool = False
    error_status: str | None = None
    last_sample_health_valid: bool | None = None
    type_mismatch: bool = False


@dataclass(frozen=True)
class _AuthoritativeHealth:
    snapshot: LocalHealthSnapshot
    expires_monotonic: float


class GatewayStore:
    """Retain only validated latest values and bounded rate timestamps."""

    def __init__(
        self,
        contract: GatewayContract,
        *,
        started_monotonic: float | None = None,
        authoritative_health_timeout_s: float | None = None,
    ) -> None:
        self.contract = contract
        self._started_monotonic = (
            time.monotonic() if started_monotonic is None else float(started_monotonic)
        )
        self._lock = threading.Lock()
        if authoritative_health_timeout_s is not None and (
            not math.isfinite(authoritative_health_timeout_s)
            or authoritative_health_timeout_s <= 0.0
        ):
            raise ValueError("authoritative_health_timeout_s must be positive")
        self._authoritative_health_timeout_s = authoritative_health_timeout_s
        self._authoritative_health: _AuthoritativeHealth | None = None
        self._retired_boot_ids: set[str] = set()
        self._topics = {
            topic.topic: _TopicState(topic)
            for source in contract.sources
            for topic in source.topics
        }
        self._health = HealthTracker(
            HealthSourceConfig(
                key=source.key,
                required=source.required,
                topics=tuple(
                    HealthTopicConfig(
                        topic=topic.topic,
                        freshness_ms=topic.freshness_ms,
                        minimum_rate_hz=topic.minimum_rate_hz,
                    )
                    for topic in source.topics
                ),
            )
            for source in contract.sources
        )

    def _topic(self, topic: str) -> _TopicState:
        try:
            return self._topics[topic]
        except KeyError as exc:
            raise KeyError(f"unknown contract topic: {topic}") from exc

    @staticmethod
    def _receive_times(
        received_monotonic: float | None,
        received_utc: datetime | None,
    ) -> tuple[float, datetime]:
        monotonic = time.monotonic() if received_monotonic is None else float(received_monotonic)
        if not math.isfinite(monotonic):
            raise ValueError("received_monotonic must be finite")
        utc = datetime.now(UTC) if received_utc is None else received_utc
        if utc.tzinfo is None:
            raise ValueError("received_utc must be timezone-aware")
        return monotonic, utc.astimezone(UTC)

    @staticmethod
    def _sample_time(
        source_timestamp: datetime | None,
        received_utc: datetime,
    ) -> tuple[datetime, str]:
        if source_timestamp is None:
            return received_utc, "gateway_receive_fallback"
        if source_timestamp.tzinfo is None:
            return received_utc, "gateway_receive_fallback"
        normalized = source_timestamp.astimezone(UTC)
        if normalized.timestamp() <= 0.0:
            return received_utc, "gateway_receive_fallback"
        return normalized, "ros_header"

    def set_type_mismatch(self, topic: str, mismatched: bool) -> None:
        with self._lock:
            self._topic(topic).type_mismatch = bool(mismatched)

    def apply_authoritative_health(
        self,
        snapshot: LocalHealthSnapshot,
        *,
        received_monotonic: float | None = None,
        received_utc: datetime | None = None,
    ) -> bool:
        received_at_monotonic = (
            time.monotonic()
            if received_monotonic is None
            else float(received_monotonic)
        )
        if not math.isfinite(received_at_monotonic):
            raise ValueError("received_monotonic must be finite")
        received_at_utc = datetime.now(UTC) if received_utc is None else received_utc
        if received_at_utc.tzinfo is None or received_at_utc.utcoffset() is None:
            raise ValueError("received_utc must be timezone-aware")
        received_at_utc = received_at_utc.astimezone(UTC)
        if self._authoritative_health_timeout_s is not None:
            snapshot_age_s = (
                received_at_utc - snapshot.snapshot_time
            ).total_seconds()
            if snapshot_age_s > self._authoritative_health_timeout_s:
                raise ValueError("authoritative health snapshot is too old")
            if snapshot_age_s < -self._authoritative_health_timeout_s:
                raise ValueError("authoritative health snapshot is too far in the future")
            expires_monotonic = (
                received_at_monotonic
                + self._authoritative_health_timeout_s
                - max(snapshot_age_s, 0.0)
            )
        else:
            expires_monotonic = math.inf
        if snapshot.agent_id != self.contract.agent_id:
            raise ValueError("authoritative health agent_id mismatch")
        if snapshot.robot_id != self.contract.manifest.robot.robot_id:
            raise ValueError("authoritative health robot_id mismatch")
        if snapshot.mapping_digest != self.contract.mapping_digest:
            raise ValueError("authoritative health mapping_digest mismatch")
        with self._lock:
            current = self._authoritative_health
            if snapshot.boot_id in self._retired_boot_ids:
                raise ValueError("authoritative health boot_id was retired")
            if current is not None:
                previous = current.snapshot
                if snapshot.boot_id == previous.boot_id:
                    if snapshot.sequence < previous.sequence:
                        raise ValueError("authoritative health sequence moved backwards")
                    if snapshot.sequence == previous.sequence:
                        if snapshot.wire_digest != previous.wire_digest:
                            raise ValueError("authoritative health sequence digest changed")
                        return False
                    if snapshot.snapshot_time <= previous.snapshot_time:
                        raise ValueError("authoritative health timestamp did not advance")
                else:
                    self._retired_boot_ids.add(previous.boot_id)
            self._authoritative_health = _AuthoritativeHealth(
                snapshot=snapshot,
                expires_monotonic=expires_monotonic,
            )
            return True

    def ingest_joint(
        self,
        topic: str,
        names: Sequence[str],
        values: Sequence[float],
        *,
        source_timestamp: datetime | None = None,
        received_monotonic: float | None = None,
        received_utc: datetime | None = None,
    ) -> bool:
        monotonic, utc = self._receive_times(received_monotonic, received_utc)
        with self._lock:
            state = self._topic(topic)
            if state.spec.kind not in {"state", "action"}:
                raise ValueError(f"topic {topic} is not a joint source")
            structurally_valid = joint_sample_is_valid(
                state.spec.features,
                names,
                values,
            )
            timestamp, provenance = self._sample_time(source_timestamp, utc)
            self._health.record(
                topic,
                valid=structurally_valid,
                received_monotonic=monotonic,
                rate_timestamp_s=(
                    timestamp.timestamp() if provenance == "ros_header" else None
                ),
            )
            state.source_timestamp = timestamp
            state.provenance = provenance
            state.type_mismatch = False
            state.last_sample_health_valid = structurally_valid

            valid = structurally_valid
            converted: dict[str, float] = {}
            if valid:
                feature_specs = {
                    feature.name: feature for feature in state.spec.feature_specs
                }
                for name, raw_value in zip(names, values, strict=True):
                    value = float(raw_value)
                    feature = feature_specs[name]
                    if (
                        not math.isfinite(value)
                        or value < feature.minimum
                        or value > feature.maximum
                    ):
                        valid = False
                        break
                    converted[name] = value

            if valid:
                state.feature_values = converted
                state.has_sample = True
                state.error_status = None
                return True
            state.feature_values = None
            state.has_sample = False
            state.error_status = "BadDecodingError"
            return False

    def ingest_camera(
        self,
        topic: str,
        message_format: str,
        *,
        source_timestamp: datetime | None = None,
        received_monotonic: float | None = None,
        received_utc: datetime | None = None,
    ) -> bool:
        monotonic, utc = self._receive_times(received_monotonic, received_utc)
        with self._lock:
            state = self._topic(topic)
            if state.spec.kind != "camera":
                raise ValueError(f"topic {topic} is not a camera source")
            valid = camera_format_is_valid(message_format)
            timestamp, provenance = self._sample_time(source_timestamp, utc)
            self._health.record(
                topic,
                valid=valid,
                received_monotonic=monotonic,
                rate_timestamp_s=(
                    timestamp.timestamp() if provenance == "ros_header" else None
                ),
            )
            state.source_timestamp = timestamp
            state.provenance = provenance
            state.type_mismatch = False
            state.last_sample_health_valid = valid
            if not valid:
                state.has_sample = False
                state.error_status = "BadDecodingError"
                return False
            state.has_sample = True
            state.error_status = None
            return True

    def _status(
        self,
        state: _TopicState,
        *,
        now_monotonic: float,
        fresh: bool,
        rate_ok: bool | None,
    ) -> str:
        if state.type_mismatch:
            return "BadTypeMismatch"
        if state.error_status is not None:
            return state.error_status
        if not state.has_sample:
            if now_monotonic - self._started_monotonic < self.contract.startup_grace_s:
                return "BadWaitingForInitialData"
            return "BadNoCommunication" if state.spec.required else "BadWaitingForInitialData"
        if not fresh:
            return "UncertainLastUsableValue"
        if rate_ok is False:
            return "UncertainDataSubNormal"
        if state.provenance == "gateway_receive_fallback":
            return "Uncertain"
        return "Good"

    def snapshot(
        self,
        *,
        now_monotonic: float | None = None,
        now_utc: datetime | None = None,
    ) -> RobotSnapshot:
        monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if not math.isfinite(monotonic):
            raise ValueError("now_monotonic must be finite")
        evaluated_at = datetime.now(UTC) if now_utc is None else now_utc.astimezone(UTC)
        with self._lock:
            local_health = self._health.snapshot(now_monotonic=monotonic)
            authoritative = self._authoritative_health
            authoritative_required = self._authoritative_health_timeout_s is not None
            authoritative_current = (
                authoritative is not None
                and authoritative_required
                and monotonic <= authoritative.expires_monotonic
            )
            authoritative_snapshot = (
                authoritative.snapshot if authoritative_current else None
            )
            if authoritative_snapshot is not None:
                evaluated_at = authoritative_snapshot.snapshot_time
            source_snapshots: dict[str, SourceSnapshot] = {}
            for source in self.contract.sources:
                topics: dict[str, TopicSnapshot] = {}
                authoritative_source = (
                    None
                    if authoritative_snapshot is None
                    else authoritative_snapshot.health.sources[source.key]
                )
                for topic_spec in source.topics:
                    state = self._topics[topic_spec.topic]
                    local_topic = local_health.sources[source.key].topics[
                        topic_spec.topic
                    ]
                    local_fresh = local_topic.fresh and not state.type_mismatch
                    status = self._status(
                        state,
                        now_monotonic=monotonic,
                        fresh=local_fresh,
                        rate_ok=local_topic.rate_ok,
                    )
                    sample = ValueSnapshot(
                        value=(
                            local_topic.sequence if state.has_sample else None
                        ),
                        status=status,
                        source_timestamp=state.source_timestamp,
                        provenance=state.provenance,
                    )
                    features = {
                        feature: ValueSnapshot(
                            value=(
                                None
                                if state.feature_values is None
                                else state.feature_values[feature]
                            ),
                            status=status,
                            source_timestamp=state.source_timestamp,
                            provenance=state.provenance,
                        )
                        for feature in topic_spec.features
                    }
                    if authoritative_source is not None:
                        projected = authoritative_source.topics[topic_spec.topic]
                        age_ms = projected.age_ms
                        rate_hz = projected.rate_hz
                        minimum_rate_hz = projected.minimum_rate_hz
                        rate_ok = projected.rate_ok
                        sequence = projected.sequence
                        fresh = projected.fresh
                    elif authoritative_required:
                        age_ms = None
                        rate_hz = None
                        minimum_rate_hz = topic_spec.minimum_rate_hz
                        rate_ok = None
                        sequence = 0
                        fresh = False
                    else:
                        age_ms = local_topic.age_ms
                        rate_hz = local_topic.rate_hz
                        minimum_rate_hz = local_topic.minimum_rate_hz
                        rate_ok = local_topic.rate_ok
                        sequence = local_topic.sequence
                        fresh = local_fresh
                    topics[topic_spec.topic] = TopicSnapshot(
                        topic=topic_spec.topic,
                        age_ms=age_ms,
                        rate_hz=rate_hz,
                        minimum_rate_hz=minimum_rate_hz,
                        rate_ok=rate_ok,
                        sequence=sequence,
                        fresh=fresh,
                        sample=sample,
                        features=features,
                    )

                local_source = local_health.sources[source.key]
                source_snapshots[source.key] = SourceSnapshot(
                    key=source.key,
                    kind=source.kind,
                    required=source.required,
                    freshness_ms=source.freshness_ms,
                    fresh=(
                        authoritative_source.fresh
                        if authoritative_source is not None
                        else (
                            False
                            if authoritative_required
                            else all(topic.fresh for topic in topics.values())
                        )
                    ),
                    rate_ok=(
                        authoritative_source.rate_ok
                        if authoritative_source is not None
                        else (
                            None
                            if authoritative_required
                            else local_source.rate_ok
                        )
                    ),
                    topics=topics,
                )

            if authoritative_snapshot is not None:
                robot_health = authoritative_snapshot.health
                fsm_state = robot_health.fsm_state
                sources_fresh = robot_health.sources_fresh
                rates_ok = robot_health.rates_ok
                fault = robot_health.fault
            elif authoritative_required:
                within_grace = (
                    monotonic - self._started_monotonic
                    < self.contract.startup_grace_s
                )
                fsm_state = "STARTING" if within_grace else "DEGRADED"
                sources_fresh = False
                rates_ok = not any(
                    source.required and source.minimum_rate_hz is not None
                    for source in self.contract.sources
                )
                fault = "authoritative health snapshot unavailable"
            else:
                required = [
                    source
                    for source in source_snapshots.values()
                    if source.required
                ]
                sources_fresh = all(source.fresh for source in required)
                rates_ok = local_health.rates_ok
                faults = []
                stale = [source.key for source in required if not source.fresh]
                low_rate = list(local_health.rate_not_ok_required)
                if stale:
                    faults.append("stale required sources: " + ", ".join(stale))
                if low_rate:
                    faults.append(
                        "low-rate required sources: " + ", ".join(low_rate)
                    )
                within_grace = (
                    monotonic - self._started_monotonic
                    < self.contract.startup_grace_s
                )
                explicit_error = any(
                    state.last_sample_health_valid is False or state.type_mismatch
                    for state in self._topics.values()
                    if state.spec.required
                )
                if sources_fresh and rates_ok:
                    fsm_state = "READY"
                elif within_grace and not explicit_error:
                    fsm_state = "STARTING"
                else:
                    fsm_state = "DEGRADED"
                fault = "; ".join(faults)
            return RobotSnapshot(
                evaluated_at=evaluated_at,
                health_evaluator_available=(
                    authoritative_snapshot is not None
                    if authoritative_required
                    else True
                ),
                fsm_state=fsm_state,
                phase="MONITORING",
                sources_fresh=sources_fresh,
                rates_ok=rates_ok,
                fault=fault,
                sources=source_snapshots,
            )
