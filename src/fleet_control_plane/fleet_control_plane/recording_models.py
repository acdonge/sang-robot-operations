"""Validated central recording-session contracts."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from fleet_control_plane.models import FleetModel, SafeId, SessionId


Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", strip_whitespace=True),
]
ArtifactPath = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def validate_artifact_path(value: str) -> str:
    if "\\" in value or "\x00" in value:
        raise ValueError("artifact paths must use canonical POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path must remain inside the session namespace")
    canonical = path.as_posix()
    if canonical != value:
        raise ValueError("artifact path must be canonical")
    if canonical == "session-manifest.json":
        raise ValueError("session-manifest.json is reserved by Central")
    return canonical


class RecordingFile(FleetModel):
    path: ArtifactPath
    size_bytes: int = Field(ge=0, le=10 * 1024**4)
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_confined(cls, value: str) -> str:
        return validate_artifact_path(value)


class SourceTimestampStats(FleetModel):
    message_count: int = Field(ge=0)
    valid_header_count: int = Field(ge=0)
    zero_header_count: int = Field(ge=0)
    timestamp_regressions: int = Field(ge=0)
    first_header_ns: int | None = Field(default=None, ge=1)
    last_header_ns: int | None = Field(default=None, ge=1)
    minimum_header_ns: int | None = Field(default=None, ge=1)
    maximum_header_ns: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def counts_and_ranges_are_consistent(self) -> "SourceTimestampStats":
        if self.valid_header_count + self.zero_header_count > self.message_count:
            raise ValueError("source timestamp counts exceed message_count")
        values = (
            self.first_header_ns,
            self.last_header_ns,
            self.minimum_header_ns,
            self.maximum_header_ns,
        )
        if self.valid_header_count == 0 and any(value is not None for value in values):
            raise ValueError("empty source timestamps cannot declare a time range")
        if self.valid_header_count > 0 and any(value is None for value in values):
            raise ValueError("valid source timestamps require a complete time range")
        if (
            self.minimum_header_ns is not None
            and self.maximum_header_ns is not None
            and self.minimum_header_ns > self.maximum_header_ns
        ):
            raise ValueError("source timestamp range is reversed")
        if self.minimum_header_ns is not None:
            if not (
                self.minimum_header_ns
                <= self.first_header_ns
                <= self.maximum_header_ns
                and self.minimum_header_ns
                <= self.last_header_ns
                <= self.maximum_header_ns
            ):
                raise ValueError("first/last source timestamps fall outside their range")
            if self.timestamp_regressions > max(0, self.valid_header_count - 1):
                raise ValueError("source timestamp regression count is impossible")
        return self


class RecordingTopic(FleetModel):
    key: SafeId
    name: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    type: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    asset_id: SafeId
    calibration_ref: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    timestamp_provenance: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512),
    ]
    message_count: int = Field(ge=1)
    source_timestamp: SourceTimestampStats

    @field_validator("name")
    @classmethod
    def topic_is_absolute(cls, value: str) -> str:
        if not value.startswith("/") or any(character.isspace() for character in value):
            raise ValueError("recorded topic names must be absolute ROS topic names")
        return value

    @model_validator(mode="after")
    def timestamp_count_matches_messages(self) -> "RecordingTopic":
        if self.source_timestamp.message_count != self.message_count:
            raise ValueError("source timestamp message_count does not match the topic")
        return self


class RecordingTimeRange(FleetModel):
    start_ns: int = Field(ge=1)
    start: AwareDatetime
    end_ns: int = Field(ge=1)
    end: AwareDatetime
    duration_ns: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def range_is_ordered(self) -> "RecordingTimeRange":
        if self.end_ns < self.start_ns or self.end < self.start:
            raise ValueError("recording time range is reversed")
        if self.duration_ns is not None and self.duration_ns != self.end_ns - self.start_ns:
            raise ValueError("recording duration does not match its nanosecond range")
        return self


class OptionalSourceTimeRange(FleetModel):
    start_ns: int | None = Field(default=None, ge=1)
    start: AwareDatetime | None = None
    end_ns: int | None = Field(default=None, ge=1)
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def optional_range_is_consistent(self) -> "OptionalSourceTimeRange":
        values = (self.start_ns, self.start, self.end_ns, self.end)
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("source time range must be entirely present or absent")
        if self.start_ns is not None and (
            self.end_ns < self.start_ns or self.end < self.start
        ):
            raise ValueError("source time range is reversed")
        return self


class TaskDefinitionReference(FleetModel):
    id: SafeId
    version: int = Field(ge=1)


class RecordingSafety(FleetModel):
    read_only_observation: Literal[True]
    robot_command_interfaces: Literal[False]
    vendor_command_channel: Literal[False]


class RecordingManifest(FleetModel):
    schema_version: Literal["1.0"]
    artifact_id: Annotated[
        str,
        StringConstraints(min_length=6, max_length=133, pattern=r"^mcap:[A-Za-z0-9][A-Za-z0-9_.-]*$"),
    ]
    session_id: SessionId
    state: Literal["complete"]
    storage_identifier: Literal["mcap"]
    profile_id: SafeId
    agent_id: SafeId
    robot_id: SafeId
    run_id: SafeId | None = None
    task_definition: TaskDefinitionReference | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    record_time: RecordingTimeRange
    source_time_range: OptionalSourceTimeRange
    clock: dict[str, Any]
    topics: list[RecordingTopic] = Field(min_length=1, max_length=128)
    files: list[RecordingFile] = Field(min_length=2, max_length=128)
    total_size_bytes: int = Field(ge=0, le=10 * 1024**4)
    checksum_algorithm: Literal["sha256"]
    parent_artifacts: list[str] = Field(max_length=0)
    provenance: dict[str, Any]
    recorder_diagnostics: dict[str, Any]
    safety: RecordingSafety

    @model_validator(mode="after")
    def manifest_is_self_consistent(self) -> "RecordingManifest":
        if self.artifact_id != f"mcap:{self.session_id}":
            raise ValueError("artifact_id must be mcap:<session_id>")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("recording artifact paths must be unique")
        if "metadata.yaml" not in paths:
            raise ValueError("recording artifacts must include metadata.yaml")
        if not any(path.endswith(".mcap") for path in paths):
            raise ValueError("recording artifacts must include an MCAP file")
        if self.total_size_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("total_size_bytes does not match artifact sizes")
        keys = [item.key for item in self.topics]
        names = [item.name for item in self.topics]
        if len(keys) != len(set(keys)) or len(names) != len(set(names)):
            raise ValueError("recording topic keys and names must be unique")
        source_minimums = [
            item.source_timestamp.minimum_header_ns
            for item in self.topics
            if item.source_timestamp.minimum_header_ns is not None
        ]
        source_maximums = [
            item.source_timestamp.maximum_header_ns
            for item in self.topics
            if item.source_timestamp.maximum_header_ns is not None
        ]
        expected_source_range = (
            (min(source_minimums), max(source_maximums))
            if source_minimums and source_maximums
            else (None, None)
        )
        actual_source_range = (
            self.source_time_range.start_ns,
            self.source_time_range.end_ns,
        )
        if actual_source_range != expected_source_range:
            raise ValueError("session source time range does not match its topics")
        return self


class RecordingFinalize(FleetModel):
    expected_manifest_sha256: Sha256
