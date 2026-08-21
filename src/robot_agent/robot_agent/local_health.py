"""Versioned local health snapshot contract shared with read-only sidecars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from numbers import Real
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

import yaml

from robot_agent.manifest import RobotManifest


UTC = timezone.utc
LEGACY_SCHEMA_VERSION = 1
AUTHENTICATED_SCHEMA_VERSION = 2
SCHEMA_VERSION = LEGACY_SCHEMA_VERSION
ALGORITHM_VERSION = "health-v1"
SIGNATURE_ALGORITHM = "hmac-sha256"
SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
WIRE_KEYS_V1 = frozenset(
    {
        "schema_version",
        "algorithm_version",
        "agent_id",
        "robot_id",
        "boot_id",
        "sequence",
        "snapshot_time_utc",
        "manifest_digest",
        "payload_digest",
        "health",
    }
)
WIRE_KEYS_V2 = WIRE_KEYS_V1 | frozenset(
    {"key_id", "signature_algorithm", "signature"}
)
HEALTH_KEYS = frozenset(
    {"fsm_state", "phase", "sources_fresh", "rates_ok", "fault", "sources"}
)
SOURCE_KEYS = frozenset({"key", "fresh", "rate_ok", "topics"})
TOPIC_KEYS = frozenset(
    {
        "topic",
        "age_ms",
        "rate_hz",
        "minimum_rate_hz",
        "rate_ok",
        "sequence",
        "fresh",
    }
)


class LocalHealthError(ValueError):
    """Raised when a local health snapshot is malformed or inconsistent."""


@dataclass(frozen=True)
class LocalTopicHealth:
    topic: str
    age_ms: float | None
    rate_hz: float | None
    minimum_rate_hz: float | None
    rate_ok: bool | None
    sequence: int
    fresh: bool


@dataclass(frozen=True)
class LocalSourceHealth:
    key: str
    fresh: bool
    rate_ok: bool | None
    topics: Mapping[str, LocalTopicHealth]


@dataclass(frozen=True)
class LocalRobotHealth:
    fsm_state: str
    phase: str
    sources_fresh: bool
    rates_ok: bool
    fault: str
    sources: Mapping[str, LocalSourceHealth]


@dataclass(frozen=True)
class LocalHealthSnapshot:
    agent_id: str
    robot_id: str
    boot_id: str
    sequence: int
    snapshot_time: datetime
    manifest_digest: str
    mapping_digest: str | None
    payload_digest: str
    wire_digest: str
    authenticated: bool
    key_id: str | None
    health: LocalRobotHealth


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise LocalHealthError(f"health snapshot is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_signing_key(value: bytes) -> bytes:
    key = bytes(value)
    if not 32 <= len(key) <= 4096:
        raise LocalHealthError("local health HMAC key must contain 32 to 4096 bytes")
    return key


def load_local_health_key(path: Path) -> bytes:
    try:
        key = path.read_bytes().strip()
    except OSError as exc:
        raise LocalHealthError(f"cannot read local health HMAC key: {exc}") from exc
    return _validated_signing_key(key)


def _signature(value: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(
        _validated_signing_key(key),
        _canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def mapping_health_digest(value: Mapping[str, Any]) -> str:
    """Return a canonical digest for file-driven OPC UA mapping metadata."""
    if not isinstance(value, Mapping):
        raise LocalHealthError("mapping contract must be a mapping")
    return _digest(value)


def load_mapping_health_digest(
    path: Path,
    *,
    expected_robot_id: str,
) -> str:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LocalHealthError(f"cannot load mapping contract: {exc}") from exc
    if not isinstance(value, Mapping):
        raise LocalHealthError("mapping contract must be a mapping")
    if value.get("schema_version") != "2.0.0":
        raise LocalHealthError("mapping digest binding requires schema_version 2.0.0")
    identity = value.get("identity")
    if not isinstance(identity, Mapping) or identity.get("robot_id") != expected_robot_id:
        raise LocalHealthError("mapping robot_id mismatch")
    return mapping_health_digest(value)


def _validated_mapping_digest(value: str | None) -> str | None:
    if value is not None and not SHA256_HEX.fullmatch(value):
        raise LocalHealthError("mapping_digest must be a lowercase SHA-256 digest")
    return value


def manifest_health_digest(
    manifest: RobotManifest,
    mapping_digest: str | None = None,
) -> str:
    mapping_digest = _validated_mapping_digest(mapping_digest)
    contract = {
        "schema_version": manifest.schema_version,
        "robot_id": manifest.robot.robot_id,
        "model": manifest.robot.model,
        "real_or_sim": manifest.robot.real_or_sim,
        "host_id": manifest.robot.host_id,
        "sources": [
            {
                "key": source.key,
                "kind": source.kind,
                "message_type": source.message_type,
                "required": source.required,
                "freshness_ms": source.freshness_ms,
                "minimum_rate_hz": source.minimum_rate_hz,
                "topics": list(source.topics),
                "topic_features": [list(values) for values in source.topic_features],
                "encoding": source.encoding,
            }
            for source in manifest.sources
        ],
    }
    if mapping_digest is not None:
        contract["mapping_digest"] = mapping_digest
    return _digest(contract)


def _health_payload(robot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fsm_state": robot["fsm_state"],
        "phase": robot["phase"],
        "sources_fresh": robot["sources_fresh"],
        "rates_ok": robot["rates_ok"],
        "fault": robot.get("fault"),
        "sources": [
            {
                "key": source["key"],
                "fresh": source["fresh"],
                "rate_ok": source.get("rate_ok"),
                "topics": [
                    {
                        "topic": topic["topic"],
                        "age_ms": topic.get("age_ms"),
                        "rate_hz": topic.get("rate_hz"),
                        "minimum_rate_hz": topic.get("minimum_rate_hz"),
                        "rate_ok": topic.get("rate_ok"),
                        "sequence": topic["seq"],
                        "fresh": topic["fresh"],
                    }
                    for topic in source["topics"]
                ],
            }
            for source in robot["sources"]
        ],
    }


def build_local_health_snapshot(
    *,
    agent_id: str,
    boot_id: str,
    sequence: int,
    snapshot_time_utc: str,
    manifest: RobotManifest,
    robot: Mapping[str, Any],
    mapping_digest: str | None = None,
    signing_key: bytes | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    if (signing_key is None) != (key_id is None):
        raise LocalHealthError("signing_key and key_id must be supplied together")
    if key_id is not None and not SAFE_KEY_ID.fullmatch(key_id):
        raise LocalHealthError("local health key_id is invalid")
    health = _health_payload(robot)
    payload = {
        "schema_version": (
            AUTHENTICATED_SCHEMA_VERSION
            if signing_key is not None
            else LEGACY_SCHEMA_VERSION
        ),
        "algorithm_version": ALGORITHM_VERSION,
        "agent_id": agent_id,
        "robot_id": manifest.robot.robot_id,
        "boot_id": boot_id,
        "sequence": sequence,
        "snapshot_time_utc": snapshot_time_utc,
        "manifest_digest": manifest_health_digest(manifest, mapping_digest),
        "payload_digest": _digest(health),
        "health": health,
    }
    if signing_key is not None:
        payload["key_id"] = key_id
        payload["signature_algorithm"] = SIGNATURE_ALGORITHM
        payload["signature"] = _signature(payload, signing_key)
    parse_local_health_snapshot(
        payload,
        expected_agent_id=agent_id,
        manifest=manifest,
        expected_mapping_digest=mapping_digest,
        verification_key=signing_key,
        expected_key_id=key_id,
        require_authenticated=signing_key is not None,
    )
    return payload


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise LocalHealthError(f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalHealthError(f"{name} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LocalHealthError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise LocalHealthError(f"{name} must be a boolean")
    return value


def _optional_boolean(value: object, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise LocalHealthError(f"{name} must be a boolean or null")
    return value


def _optional_nonnegative(value: object, name: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, Real)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0.0
    ):
        raise LocalHealthError(f"{name} must be a non-negative finite number or null")
    return float(value)


def _rate_equal(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)


def _source_rate_ok(topics: tuple[LocalTopicHealth, ...]) -> bool | None:
    configured = [topic for topic in topics if topic.minimum_rate_hz is not None]
    if not configured:
        return None
    if any(topic.rate_ok is False for topic in configured):
        return False
    if any(topic.rate_ok is None for topic in configured):
        return None
    return True


def parse_local_health_snapshot(
    value: bytes | str | Mapping[str, Any],
    *,
    expected_agent_id: str,
    manifest: RobotManifest,
    expected_mapping_digest: str | None = None,
    verification_key: bytes | None = None,
    expected_key_id: str | None = None,
    require_authenticated: bool = False,
) -> LocalHealthSnapshot:
    if isinstance(value, bytes):
        try:
            raw = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalHealthError("health snapshot is not valid UTF-8 JSON") from exc
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LocalHealthError("health snapshot is not valid JSON") from exc
    else:
        raw = value
    if not isinstance(raw, Mapping):
        raise LocalHealthError("health snapshot envelope must be an object")
    schema_version = raw.get("schema_version")
    if schema_version == LEGACY_SCHEMA_VERSION:
        if set(raw) != WIRE_KEYS_V1:
            raise LocalHealthError("health snapshot envelope keys differ from schema v1")
        if require_authenticated:
            raise LocalHealthError("authenticated local health snapshot is required")
        authenticated = False
        key_id = None
    elif schema_version == AUTHENTICATED_SCHEMA_VERSION:
        if set(raw) != WIRE_KEYS_V2:
            raise LocalHealthError("health snapshot envelope keys differ from schema v2")
        key_id = raw.get("key_id")
        if not isinstance(key_id, str) or not SAFE_KEY_ID.fullmatch(key_id):
            raise LocalHealthError("local health key_id is invalid")
        if expected_key_id is not None and not hmac.compare_digest(
            key_id,
            expected_key_id,
        ):
            raise LocalHealthError("local health key_id mismatch")
        if verification_key is None:
            raise LocalHealthError("local health verification key is required")
        if raw.get("signature_algorithm") != SIGNATURE_ALGORITHM:
            raise LocalHealthError("unsupported local health signature algorithm")
        supplied_signature = raw.get("signature")
        if not isinstance(supplied_signature, str):
            raise LocalHealthError("local health signature is invalid")
        signed = dict(raw)
        del signed["signature"]
        if not hmac.compare_digest(
            supplied_signature,
            _signature(signed, verification_key),
        ):
            raise LocalHealthError("local health signature mismatch")
        authenticated = True
    else:
        raise LocalHealthError("unsupported local health schema_version")
    if raw.get("algorithm_version") != ALGORITHM_VERSION:
        raise LocalHealthError("unsupported local health algorithm_version")
    if raw.get("agent_id") != expected_agent_id:
        raise LocalHealthError("local health agent_id mismatch")
    if raw.get("robot_id") != manifest.robot.robot_id:
        raise LocalHealthError("local health robot_id mismatch")
    try:
        boot_id = str(UUID(str(raw.get("boot_id"))))
    except ValueError as exc:
        raise LocalHealthError("boot_id must be a UUID") from exc
    if boot_id != raw.get("boot_id"):
        raise LocalHealthError("boot_id must use canonical UUID form")
    sequence = raw.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise LocalHealthError("sequence must be a positive integer")
    snapshot_time = _aware_datetime(raw.get("snapshot_time_utc"), "snapshot_time_utc")
    expected_mapping_digest = _validated_mapping_digest(expected_mapping_digest)
    expected_manifest_digest = manifest_health_digest(
        manifest,
        expected_mapping_digest,
    )
    if not hmac.compare_digest(str(raw.get("manifest_digest")), expected_manifest_digest):
        raise LocalHealthError("manifest_digest mismatch")
    health = raw.get("health")
    if not isinstance(health, Mapping) or set(health) != HEALTH_KEYS:
        raise LocalHealthError("health keys differ from schema v1")
    payload_digest = str(raw.get("payload_digest"))
    if not hmac.compare_digest(payload_digest, _digest(health)):
        raise LocalHealthError("payload_digest mismatch")

    raw_sources = health.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != len(manifest.sources):
        raise LocalHealthError("health sources do not match manifest")
    sources: dict[str, LocalSourceHealth] = {}
    for source_config, raw_source in zip(manifest.sources, raw_sources, strict=True):
        if (
            not isinstance(raw_source, Mapping)
            or set(raw_source) != SOURCE_KEYS
            or raw_source.get("key") != source_config.key
        ):
            raise LocalHealthError("health source order or identity differs from manifest")
        raw_topics = raw_source.get("topics")
        if not isinstance(raw_topics, list) or len(raw_topics) != len(source_config.topics):
            raise LocalHealthError(f"source {source_config.key} topics differ from manifest")
        topics: dict[str, LocalTopicHealth] = {}
        for topic_name, raw_topic in zip(source_config.topics, raw_topics, strict=True):
            if (
                not isinstance(raw_topic, Mapping)
                or set(raw_topic) != TOPIC_KEYS
                or raw_topic.get("topic") != topic_name
            ):
                raise LocalHealthError(
                    f"source {source_config.key} topic order differs from manifest"
                )
            minimum = _optional_nonnegative(
                raw_topic.get("minimum_rate_hz"),
                f"{source_config.key}.{topic_name}.minimum_rate_hz",
            )
            if not _rate_equal(minimum, source_config.minimum_rate_hz):
                raise LocalHealthError(
                    f"source {source_config.key} minimum_rate_hz differs from manifest"
                )
            rate_hz = _optional_nonnegative(
                raw_topic.get("rate_hz"),
                f"{source_config.key}.{topic_name}.rate_hz",
            )
            rate_ok = _optional_boolean(
                raw_topic.get("rate_ok"),
                f"{source_config.key}.{topic_name}.rate_ok",
            )
            measurable = minimum is not None and rate_hz is not None
            if (measurable and rate_ok is None) or (not measurable and rate_ok is not None):
                raise LocalHealthError(
                    f"source {source_config.key} topic rate fields are inconsistent"
                )
            if measurable and rate_ok != (rate_hz >= minimum):
                raise LocalHealthError(
                    f"source {source_config.key} topic rate_ok is inconsistent"
                )
            topic_sequence = raw_topic.get("sequence")
            if (
                not isinstance(topic_sequence, int)
                or isinstance(topic_sequence, bool)
                or topic_sequence < 0
            ):
                raise LocalHealthError("topic sequence must be a non-negative integer")
            topics[topic_name] = LocalTopicHealth(
                topic=topic_name,
                age_ms=_optional_nonnegative(
                    raw_topic.get("age_ms"),
                    f"{source_config.key}.{topic_name}.age_ms",
                ),
                rate_hz=rate_hz,
                minimum_rate_hz=minimum,
                rate_ok=rate_ok,
                sequence=topic_sequence,
                fresh=_boolean(
                    raw_topic.get("fresh"),
                    f"{source_config.key}.{topic_name}.fresh",
                ),
            )
        topic_values = tuple(topics.values())
        source_fresh = _boolean(
            raw_source.get("fresh"),
            f"{source_config.key}.fresh",
        )
        source_rate_ok = _optional_boolean(
            raw_source.get("rate_ok"),
            f"{source_config.key}.rate_ok",
        )
        if source_fresh != all(topic.fresh for topic in topic_values):
            raise LocalHealthError(f"source {source_config.key} fresh is inconsistent")
        if source_rate_ok != _source_rate_ok(topic_values):
            raise LocalHealthError(f"source {source_config.key} rate_ok is inconsistent")
        sources[source_config.key] = LocalSourceHealth(
            key=source_config.key,
            fresh=source_fresh,
            rate_ok=source_rate_ok,
            topics=MappingProxyType(topics),
        )

    required = [
        sources[source.key] for source in manifest.sources if source.required
    ]
    sources_fresh = _boolean(health.get("sources_fresh"), "sources_fresh")
    if sources_fresh != all(source.fresh for source in required):
        raise LocalHealthError("robot sources_fresh is inconsistent")
    rate_required = [
        sources[source.key]
        for source in manifest.sources
        if source.required and source.minimum_rate_hz is not None
    ]
    rates_ok = _boolean(health.get("rates_ok"), "rates_ok")
    if rates_ok != all(source.rate_ok is True for source in rate_required):
        raise LocalHealthError("robot rates_ok is inconsistent")
    fsm_state = health.get("fsm_state")
    expected_fsm = "READY" if sources_fresh and rates_ok else "DEGRADED"
    if fsm_state != expected_fsm:
        raise LocalHealthError("robot fsm_state is inconsistent")
    if health.get("phase") != "MONITORING":
        raise LocalHealthError("robot phase must be MONITORING")
    fault_value = health.get("fault")
    if fault_value is not None and not isinstance(fault_value, str):
        raise LocalHealthError("robot fault must be a string or null")
    stale = [source.key for source in manifest.sources if source.required and not sources[source.key].fresh]
    rate_bad = [
        source.key
        for source in manifest.sources
        if source.required
        and source.minimum_rate_hz is not None
        and sources[source.key].rate_ok is not True
    ]
    fault_parts = []
    if stale:
        fault_parts.append("stale required sources: " + ", ".join(stale))
    if rate_bad:
        fault_parts.append("low-rate required sources: " + ", ".join(rate_bad))
    expected_fault = "; ".join(fault_parts)
    if (fault_value or "") != expected_fault:
        raise LocalHealthError("robot fault is inconsistent")

    return LocalHealthSnapshot(
        agent_id=expected_agent_id,
        robot_id=manifest.robot.robot_id,
        boot_id=boot_id,
        sequence=sequence,
        snapshot_time=snapshot_time,
        manifest_digest=expected_manifest_digest,
        mapping_digest=expected_mapping_digest,
        payload_digest=payload_digest,
        wire_digest=_digest(raw),
        authenticated=authenticated,
        key_id=key_id,
        health=LocalRobotHealth(
            fsm_state=fsm_state,
            phase="MONITORING",
            sources_fresh=sources_fresh,
            rates_ok=rates_ok,
            fault=fault_value or "",
            sources=MappingProxyType(sources),
        ),
    )
