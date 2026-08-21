"""Shared, transport-independent source health calculations."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from numbers import Real
from types import MappingProxyType
import threading
import time
from typing import Iterable, Mapping, Sequence


RATE_WINDOW_S = 5.0
MAX_RATE_SAMPLES = 512
CAMERA_FORMAT_ALIASES = frozenset({"jpeg", "jpg", "mjpg"})


def joint_sample_is_valid(
    expected_names: Sequence[str],
    names: Sequence[str],
    values: Sequence[object],
) -> bool:
    """Return whether a joint sample matches its structural contract."""
    expected = tuple(expected_names)
    observed = tuple(names)
    if (
        not expected
        or any(not isinstance(name, str) or not name for name in expected)
        or len(expected) != len(set(expected))
        or observed != expected
        or len(values) != len(expected)
    ):
        return False
    return all(
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in values
    )


def camera_format_is_valid(message_format: object) -> bool:
    """Return whether a camera format is an accepted JPEG alias."""
    if not isinstance(message_format, str):
        return False
    normalized = message_format.strip().lower()
    if normalized in CAMERA_FORMAT_ALIASES:
        return True
    parts = normalized.split(";", 1)
    if len(parts) != 2:
        return False
    descriptor = parts[1].strip().split()
    return (
        len(descriptor) >= 2
        and descriptor[0] in CAMERA_FORMAT_ALIASES
        and descriptor[1] == "compressed"
    )


def _finite_timestamp(value: float | None, *, name: str) -> float:
    timestamp = time.monotonic() if value is None else value
    if (
        not isinstance(timestamp, Real)
        or isinstance(timestamp, bool)
        or not math.isfinite(timestamp)
    ):
        raise ValueError(f"{name} must be finite")
    return float(timestamp)


@dataclass(frozen=True)
class HealthTopicConfig:
    topic: str
    freshness_ms: int
    minimum_rate_hz: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str) or not self.topic:
            raise ValueError("topic must be a non-empty string")
        if (
            not isinstance(self.freshness_ms, int)
            or isinstance(self.freshness_ms, bool)
            or self.freshness_ms <= 0
        ):
            raise ValueError("freshness_ms must be a positive integer")
        if self.minimum_rate_hz is not None and (
            not isinstance(self.minimum_rate_hz, Real)
            or isinstance(self.minimum_rate_hz, bool)
            or not math.isfinite(self.minimum_rate_hz)
            or self.minimum_rate_hz <= 0.0
        ):
            raise ValueError("minimum_rate_hz must be a positive finite number")


@dataclass(frozen=True)
class HealthSourceConfig:
    key: str
    required: bool
    topics: tuple[HealthTopicConfig, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("source key must be a non-empty string")
        if not isinstance(self.required, bool):
            raise ValueError("required must be a boolean")
        if not self.topics:
            raise ValueError("source must contain at least one topic")
        names = [topic.topic for topic in self.topics]
        if len(names) != len(set(names)):
            raise ValueError(f"source {self.key!r} contains duplicate topics")


@dataclass(frozen=True)
class TopicHealth:
    topic: str
    age_ms: float | None
    rate_hz: float | None
    minimum_rate_hz: float | None
    rate_ok: bool | None
    sequence: int
    fresh: bool
    last_sample_valid: bool | None


@dataclass(frozen=True)
class SourceHealth:
    key: str
    required: bool
    fresh: bool
    rate_ok: bool | None
    topics: Mapping[str, TopicHealth]


@dataclass(frozen=True)
class RobotHealth:
    sources_fresh: bool
    rates_ok: bool
    stale_required: tuple[str, ...]
    rate_not_ok_required: tuple[str, ...]
    sources: Mapping[str, SourceHealth]


@dataclass
class _TopicState:
    config: HealthTopicConfig
    sequence: int = 0
    last_received: float | None = None
    last_sample_valid: bool | None = None
    recent: deque[float] = field(
        default_factory=lambda: deque(maxlen=MAX_RATE_SAMPLES)
    )


class HealthTracker:
    """Track receipt health while leaving values and status codes to callers."""

    def __init__(self, sources: Iterable[HealthSourceConfig]) -> None:
        configs = tuple(sources)
        source_keys = [source.key for source in configs]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source keys must be unique")

        topic_configs: dict[str, HealthTopicConfig] = {}
        for source in configs:
            for topic in source.topics:
                if topic.topic in topic_configs:
                    raise ValueError(
                        f"topic {topic.topic!r} belongs to multiple sources"
                    )
                topic_configs[topic.topic] = topic

        self._sources = configs
        self._topics = {
            topic: _TopicState(config)
            for topic, config in topic_configs.items()
        }
        self._lock = threading.Lock()

    def record(
        self,
        topic: str,
        *,
        valid: bool,
        received_monotonic: float | None = None,
        rate_timestamp_s: float | None = None,
    ) -> None:
        """Record an event using receipt time for age and sample time for rate."""
        received_at = _finite_timestamp(
            received_monotonic,
            name="received_monotonic",
        )
        rate_at = (
            received_at
            if rate_timestamp_s is None
            else _finite_timestamp(rate_timestamp_s, name="rate_timestamp_s")
        )
        if not isinstance(valid, bool):
            raise ValueError("valid must be a boolean")
        try:
            state = self._topics[topic]
        except KeyError as exc:
            raise KeyError(f"unknown health topic: {topic}") from exc

        with self._lock:
            if (
                state.last_received is not None
                and received_at < state.last_received
            ):
                raise ValueError(
                    f"received_monotonic moved backwards for topic {topic!r}"
                )
            if state.last_received is not None and (
                received_at - state.last_received > RATE_WINDOW_S
            ):
                state.recent.clear()
            elif state.recent and rate_at <= state.recent[-1]:
                state.recent.clear()
            state.sequence += 1
            state.last_received = received_at
            state.last_sample_valid = valid
            state.recent.append(rate_at)

    @staticmethod
    def _topic_health(state: _TopicState, now: float) -> TopicHealth:
        if state.last_received is not None and now < state.last_received:
            raise ValueError(
                f"now_monotonic precedes the last event for topic "
                f"{state.config.topic!r}"
            )

        age_s = (
            None
            if state.last_received is None
            else now - state.last_received
        )
        if age_s is None or age_s > RATE_WINDOW_S:
            rate_hz = None
        else:
            cutoff = state.recent[-1] - RATE_WINDOW_S
            while len(state.recent) > 1 and state.recent[0] < cutoff:
                state.recent.popleft()
            duration = (
                state.recent[-1] - state.recent[0]
                if len(state.recent) > 1
                else 0.0
            )
            rate_hz = (
                (len(state.recent) - 1) / duration
                if duration > 0.0
                else None
            )
        rate_ok = (
            None
            if rate_hz is None or state.config.minimum_rate_hz is None
            else rate_hz >= state.config.minimum_rate_hz
        )
        age_ms = (
            None
            if age_s is None
            else age_s * 1000.0
        )
        return TopicHealth(
            topic=state.config.topic,
            age_ms=age_ms,
            rate_hz=rate_hz,
            minimum_rate_hz=state.config.minimum_rate_hz,
            rate_ok=rate_ok,
            sequence=state.sequence,
            fresh=(
                state.last_sample_valid is True
                and age_ms is not None
                and age_ms <= state.config.freshness_ms
            ),
            last_sample_valid=state.last_sample_valid,
        )

    def snapshot(self, *, now_monotonic: float | None = None) -> RobotHealth:
        """Evaluate current health without applying startup or FSM policy."""
        now = _finite_timestamp(now_monotonic, name="now_monotonic")
        with self._lock:
            topic_health = {
                topic: self._topic_health(state, now)
                for topic, state in self._topics.items()
            }
            source_health: dict[str, SourceHealth] = {}
            for source in self._sources:
                topics = {
                    config.topic: topic_health[config.topic]
                    for config in source.topics
                }
                rate_topics = [
                    topic
                    for topic in topics.values()
                    if topic.minimum_rate_hz is not None
                ]
                if not rate_topics:
                    rate_ok = None
                elif any(topic.rate_ok is False for topic in rate_topics):
                    rate_ok = False
                elif any(topic.rate_ok is None for topic in rate_topics):
                    rate_ok = None
                else:
                    rate_ok = True
                source_health[source.key] = SourceHealth(
                    key=source.key,
                    required=source.required,
                    fresh=all(topic.fresh for topic in topics.values()),
                    rate_ok=rate_ok,
                    topics=MappingProxyType(topics),
                )

            required = [
                source
                for source in source_health.values()
                if source.required
            ]
            rate_required = [
                source
                for source in required
                if any(
                    topic.minimum_rate_hz is not None
                    for topic in source.topics.values()
                )
            ]
            stale_required = tuple(
                source.key for source in required if not source.fresh
            )
            rate_not_ok_required = tuple(
                source.key
                for source in rate_required
                if source.rate_ok is not True
            )
            return RobotHealth(
                sources_fresh=not stale_required,
                rates_ok=not rate_not_ok_required,
                stale_required=stale_required,
                rate_not_ok_required=rate_not_ok_required,
                sources=MappingProxyType(source_health),
            )
