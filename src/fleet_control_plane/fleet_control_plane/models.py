"""Versioned payload contract for Agent-to-Control-Plane heartbeats."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Mapping
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


MAX_ROBOTS_PER_AGENT = 64
MAX_SOURCES_PER_ROBOT = 128
MAX_TOPICS_PER_SOURCE = 64
MAX_RECENT_RECORDINGS = 20
SUPPORTED_HEARTBEAT_SCHEMA_VERSIONS = frozenset({1, 2, 3})
SECURE_HEARTBEAT_SCHEMA_VERSIONS = frozenset({2, 3})
RuntimeKind = Literal["physical", "simulation", "replay"]
SafeId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strip_whitespace=True,
    ),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
SessionId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strip_whitespace=True,
    ),
]


class FleetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


class StorageSnapshot(FleetModel):
    disk_free_gb: float | None = Field(default=None, ge=0.0)
    ram_free_gb: float | None = Field(default=None, ge=0.0)
    cpu_pct: float | None = Field(default=None, ge=0.0, le=100.0)


class DatasetSnapshot(FleetModel):
    count: int = Field(ge=0)
    total_episodes: int = Field(ge=0)
    total_size_mb: float = Field(ge=0.0)


class RecordingSessionSnapshot(FleetModel):
    session_id: SessionId
    state: Literal["complete", "failed", "partial", "invalid"]
    profile_id: SafeId | None = None
    robot_id: SafeId | None = None
    run_id: SafeId | None = None
    task_definition_id: SafeId | None = None
    task_definition_version: int | None = Field(default=None, ge=1)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    total_size_bytes: int | None = Field(default=None, ge=0)
    topic_count: int | None = Field(default=None, ge=0, le=128)
    message_count: int | None = Field(default=None, ge=0)
    transport_messages_lost: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def references_and_time_are_consistent(self) -> "RecordingSessionSnapshot":
        if (self.task_definition_id is None) != (
            self.task_definition_version is None
        ):
            raise ValueError("task definition id and version must be supplied together")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("recording finished_at cannot precede started_at")
        return self


class RecordingInventorySnapshot(FleetModel):
    enabled: bool = False
    state: Literal["disabled", "ready", "degraded"] = "disabled"
    error_code: Literal[
        "capture_root_unavailable",
        "capture_root_unreadable",
        "invalid_session_metadata",
        "inventory_scan_failed",
    ] | None = None
    complete_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    partial_count: int = Field(default=0, ge=0)
    invalid_count: int = Field(default=0, ge=0)
    scanned_count: int = Field(default=0, ge=0, le=1_000)
    total_size_bytes: int = Field(default=0, ge=0)
    truncated: bool = False
    recent: list[RecordingSessionSnapshot] = Field(
        default_factory=list,
        max_length=MAX_RECENT_RECORDINGS,
    )

    @model_validator(mode="after")
    def inventory_is_consistent(self) -> "RecordingInventorySnapshot":
        counted = (
            self.complete_count
            + self.failed_count
            + self.partial_count
            + self.invalid_count
        )
        if self.scanned_count != counted:
            raise ValueError("recording scanned_count must match state counts")
        if len(self.recent) > self.scanned_count:
            raise ValueError("recent recordings cannot exceed scanned recordings")
        session_ids = [session.session_id for session in self.recent]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("recent recording session ids must be unique")
        if not self.enabled:
            if self.state != "disabled" or counted or self.recent or self.error_code:
                raise ValueError("disabled recording inventory must be empty")
        elif self.state == "disabled":
            raise ValueError("enabled recording inventory cannot be disabled")
        if self.state == "ready" and (self.error_code is not None or self.invalid_count):
            raise ValueError("ready recording inventory cannot contain invalid metadata")
        if self.state == "degraded" and self.error_code is None:
            raise ValueError("degraded recording inventory requires an error code")
        return self


class SyncSnapshot(FleetModel):
    supported: bool = False
    state: Literal[
        "unsupported",
        "unknown",
        "locked",
        "degraded",
        "unsynchronized",
    ] = "unsupported"
    pending: None = None
    source: ShortText | None = None
    sampled_at: AwareDatetime | None = None
    reference_id: ShortText | None = None
    reference_name: ShortText | None = None
    stratum: int | None = Field(default=None, ge=1, le=16)
    system_offset_ms: float | None = None
    last_offset_ms: float | None = None
    rms_offset_ms: float | None = Field(default=None, ge=0.0)
    root_delay_ms: float | None = Field(default=None, ge=0.0)
    root_dispersion_ms: float | None = Field(default=None, ge=0.0)
    update_interval_s: float | None = Field(default=None, ge=0.0)
    leap_status: ShortText | None = None
    error_code: Literal[
        "timeout",
        "command_unavailable",
        "query_failed",
        "invalid_response",
    ] | None = None

    @model_validator(mode="after")
    def support_matches_state(self) -> "SyncSnapshot":
        details = (
            self.source,
            self.sampled_at,
            self.reference_id,
            self.reference_name,
            self.stratum,
            self.system_offset_ms,
            self.last_offset_ms,
            self.rms_offset_ms,
            self.root_delay_ms,
            self.root_dispersion_ms,
            self.update_interval_s,
            self.leap_status,
            self.error_code,
        )
        if not self.supported:
            if self.state != "unsupported" or any(item is not None for item in details):
                raise ValueError("unsupported sync status cannot contain measurements")
            return self
        if self.state == "unsupported" or self.source is None or self.sampled_at is None:
            raise ValueError("supported sync status requires a source and sample time")
        if self.state == "unknown" and self.error_code is None:
            raise ValueError("unknown sync status requires an error code")
        if self.state != "unknown" and self.error_code is not None:
            raise ValueError("measured sync status cannot contain an error code")
        return self


class RecordingSnapshot(FleetModel):
    active: bool
    episode_index: int = Field(ge=0)
    episodes_total: int = Field(ge=0)
    frames: int = Field(ge=0)
    elapsed_s: float = Field(ge=0.0)
    current_instruction: str | None = Field(default=None, max_length=4096)


class TopicSnapshot(FleetModel):
    topic: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    age_ms: float | None = Field(default=None, ge=0.0)
    rate_hz: float | None = Field(default=None, ge=0.0)
    minimum_rate_hz: float | None = Field(default=None, gt=0.0)
    rate_ok: bool | None = None
    seq: int = Field(ge=0)
    fresh: bool

    @model_validator(mode="after")
    def rate_health_is_consistent(self) -> "TopicSnapshot":
        measurable = self.minimum_rate_hz is not None and self.rate_hz is not None
        if not measurable and self.rate_ok is not None:
            raise ValueError(
                "rate_ok must be null without rate_hz and minimum_rate_hz"
            )
        if measurable and self.rate_ok is None:
            raise ValueError(
                "rate_ok is required with rate_hz and minimum_rate_hz"
            )
        return self


class SourceSnapshot(FleetModel):
    key: ShortText
    kind: Literal["state", "action", "camera"]
    required: bool
    freshness_ms: int = Field(gt=0)
    fresh: bool
    rate_ok: bool | None = None
    preview_url: Annotated[str, StringConstraints(max_length=2048)] | None = None
    topics: list[TopicSnapshot] = Field(min_length=1, max_length=MAX_TOPICS_PER_SOURCE)

    @model_validator(mode="after")
    def freshness_is_consistent(self) -> "SourceSnapshot":
        if self.preview_url is not None and self.kind != "camera":
            raise ValueError("preview_url is only valid for camera sources")
        topic_names = [topic.topic for topic in self.topics]
        if len(topic_names) != len(set(topic_names)):
            raise ValueError("source topic values must be unique")
        if self.fresh != all(topic.fresh for topic in self.topics):
            raise ValueError("source fresh must match all topic freshness values")
        rate_topics = [
            topic for topic in self.topics if topic.minimum_rate_hz is not None
        ]
        if not rate_topics:
            expected_rate_ok = None
        elif any(topic.rate_ok is False for topic in rate_topics):
            expected_rate_ok = False
        elif any(topic.rate_ok is None for topic in rate_topics):
            expected_rate_ok = None
        else:
            expected_rate_ok = True
        if self.rate_ok != expected_rate_ok:
            raise ValueError("source rate_ok must match configured topic rates")
        return self


class RobotSnapshot(FleetModel):
    robot_id: SafeId
    model: ShortText
    runtime_kind: RuntimeKind | None = None
    fsm_state: ShortText
    phase: ShortText
    sources_fresh: bool
    rates_ok: bool = True
    sources: list[SourceSnapshot] = Field(
        default_factory=list,
        max_length=MAX_SOURCES_PER_ROBOT,
    )
    fault: str | None = Field(default=None, max_length=2048)
    recording: RecordingSnapshot

    @model_validator(mode="after")
    def recording_matches_state(self) -> "RobotSnapshot":
        expected = self.fsm_state == "RECORDING"
        if self.recording.active != expected:
            raise ValueError(
                "recording.active must be true exactly when fsm_state is RECORDING"
            )
        source_keys = [source.key for source in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source key values must be unique within a robot")
        required_sources_fresh = all(
            source.fresh for source in self.sources if source.required
        )
        if self.sources and self.sources_fresh != required_sources_fresh:
            raise ValueError(
                "sources_fresh must match all required source freshness values"
            )
        rate_required_sources = [
            source
            for source in self.sources
            if source.required
            and any(
                topic.minimum_rate_hz is not None for topic in source.topics
            )
        ]
        required_rates_ok = all(
            source.rate_ok is True for source in rate_required_sources
        )
        if self.sources and self.rates_ok != required_rates_ok:
            raise ValueError(
                "rates_ok must match all configured required source rates"
            )
        return self


class AgentHeartbeatV1(FleetModel):
    schema_version: int = Field(ge=1)
    agent_id: SafeId
    agent_name: ShortText
    sw_version: ShortText
    sent_at: AwareDatetime
    boot_id: UUID | None = None
    sequence: int | None = Field(default=None, ge=1)
    storage: StorageSnapshot
    datasets: DatasetSnapshot
    recordings: RecordingInventorySnapshot = Field(
        default_factory=RecordingInventorySnapshot
    )
    sync: SyncSnapshot = Field(default_factory=SyncSnapshot)
    robots: list[RobotSnapshot] = Field(max_length=MAX_ROBOTS_PER_AGENT)

    @model_validator(mode="before")
    @classmethod
    def runtime_kind_wire_shape_is_versioned(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        schema_version = value.get("schema_version")
        robots = value.get("robots")
        if not isinstance(robots, list):
            return value
        if schema_version in {1, 2} and any(
            isinstance(robot, Mapping) and "runtime_kind" in robot
            for robot in robots
        ):
            raise ValueError(
                f"heartbeat v{schema_version} must not contain runtime_kind"
            )
        if schema_version == 3 and any(
            not isinstance(robot, Mapping) or "runtime_kind" not in robot
            for robot in robots
        ):
            raise ValueError("heartbeat v3 requires runtime_kind for every robot")
        return value

    @model_validator(mode="after")
    def robot_ids_are_unique(self) -> "AgentHeartbeatV1":
        robot_ids = [robot.robot_id for robot in self.robots]
        if len(robot_ids) != len(set(robot_ids)):
            raise ValueError("robot_id values must be unique within an Agent heartbeat")
        if self.schema_version not in SUPPORTED_HEARTBEAT_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported heartbeat schema_version: {self.schema_version}")
        if self.schema_version == 1 and (
            self.boot_id is not None or self.sequence is not None
        ):
            raise ValueError("heartbeat v1 must not contain boot_id or sequence")
        if self.schema_version in SECURE_HEARTBEAT_SCHEMA_VERSIONS and (
            self.boot_id is None or self.sequence is None
        ):
            raise ValueError(
                f"heartbeat v{self.schema_version} requires boot_id and sequence"
            )
        runtime_kinds = [robot.runtime_kind for robot in self.robots]
        if self.schema_version in {1, 2} and any(
            value is not None for value in runtime_kinds
        ):
            raise ValueError(
                f"heartbeat v{self.schema_version} must not contain runtime_kind"
            )
        if self.schema_version == 3 and any(
            value is None for value in runtime_kinds
        ):
            raise ValueError("heartbeat v3 requires runtime_kind for every robot")
        return self

    def sent_at_utc(self) -> datetime:
        return self.sent_at
