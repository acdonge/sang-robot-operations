"""Load and cross-check the frozen OPC UA and robot manifest contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import yaml

from robot_agent.local_health import mapping_health_digest
from robot_agent.manifest import RobotManifest, load_manifest


MODEL_NAMESPACE_URI = "urn:sang:fleet:model:1"
EDGE_NAMESPACE_URI = "urn:sang:fleet:edge:1"
EXPECTED_PROFILE = "sang-readonly-observation-v1"
SUPPORTED_MAPPING_SCHEMA_VERSIONS = frozenset({"1.0.0", "2.0.0"})
AGENT_ID = re.compile(r"^[A-Za-z0-9._~-]+$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ContractError(ValueError):
    """Raised when Phase 1 inputs diverge from the frozen Phase 0 contract."""


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    order: int
    selector_type: str
    selector_value: str
    opcua_data_type: str
    engineering_unit: str
    minimum: float
    maximum: float
    semantic: str


@dataclass(frozen=True)
class TopicSpec:
    source_key: str
    kind: str
    topic: str
    message_type: str
    required: bool
    freshness_ms: int
    minimum_rate_hz: float | None
    features: tuple[str, ...]
    feature_specs: tuple[FeatureSpec, ...] = ()


@dataclass(frozen=True)
class SourceSpec:
    key: str
    kind: str
    required: bool
    message_type: str
    freshness_ms: int
    minimum_rate_hz: float | None
    semantic: str | None
    topics: tuple[TopicSpec, ...]
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    configured_fps: float | None = None


@dataclass(frozen=True)
class NodeIds:
    agent_id: str
    robot_id: str

    @property
    def agent(self) -> str:
        return f"agent/{encode_component(self.agent_id)}"

    @property
    def robot(self) -> str:
        return f"{self.agent}/robot/{encode_component(self.robot_id)}"

    def robot_field(self, field: str) -> str:
        return f"{self.robot}/field/{field}"

    @property
    def sources(self) -> str:
        return f"{self.robot}/sources"

    @property
    def clock(self) -> str:
        return f"{self.robot}/clock"

    def clock_field(self, field: str) -> str:
        return f"{self.clock}/field/{field}"

    def source(self, source_key: str) -> str:
        return f"{self.robot}/source/{encode_component(source_key)}"

    def source_field(self, source_key: str, field: str) -> str:
        return f"{self.source(source_key)}/field/{field}"

    def topic(self, source_key: str, topic: str) -> str:
        return f"{self.source(source_key)}/topic/{encode_component(topic)}"

    def topic_field(self, source_key: str, topic: str, field: str) -> str:
        return f"{self.topic(source_key, topic)}/field/{field}"

    def feature(self, source_key: str, feature: str) -> str:
        return f"{self.source(source_key)}/feature/{encode_component(feature)}"

    def feature_field(self, source_key: str, feature: str, field: str) -> str:
        return f"{self.feature(source_key, feature)}/field/{field}"


@dataclass(frozen=True)
class GatewayContract:
    agent_id: str
    application_uri: str
    endpoint: str
    manifest: RobotManifest
    mapping_schema_version: str
    mapping_digest: str | None
    model_namespace_uri: str
    edge_namespace_uri: str
    nodeset_path: Path
    sources: tuple[SourceSpec, ...]
    status_codes: Mapping[str, int]
    startup_grace_s: float
    authoritative_health_timeout_s: float
    minimum_matched_samples: int
    maximum_unmatched_ratio: float
    required_timestamp_provenance: str
    preview_base_url: str | None
    maximum_node_id_bytes: int

    @property
    def node_ids(self) -> NodeIds:
        return NodeIds(self.agent_id, self.manifest.robot.robot_id)

    @property
    def topic_specs(self) -> Mapping[str, TopicSpec]:
        return {
            topic.topic: topic
            for source in self.sources
            for topic in source.topics
        }

    @property
    def source_specs(self) -> Mapping[str, SourceSpec]:
        return {source.key: source for source in self.sources}

    def feature_spec(self, source_key: str, feature: str) -> FeatureSpec:
        source = self.source_specs[source_key]
        for topic in source.topics:
            for spec in topic.feature_specs:
                if spec.name == feature:
                    return spec
        raise KeyError(f"unknown feature {source_key}.{feature}")

    def stream_uri(self, source_key: str) -> str:
        if self.preview_base_url is None:
            return ""
        robot = quote(self.manifest.robot.robot_id, safe="")
        source = quote(source_key, safe="")
        return f"{self.preview_base_url}/robots/{robot}/cameras/{source}.mjpg"


def encode_component(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("NodeId components must be non-empty strings")
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def require_loopback_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ContractError("OPC UA endpoint must use opc.tcp on a loopback host")
    if parsed.port is None or parsed.port <= 0 or parsed.port > 65535:
        raise ContractError("OPC UA endpoint must include a valid non-zero port")
    return endpoint


def require_secure_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or parsed.hostname is None:
        raise ContractError("secure OPC UA endpoint must use opc.tcp with a host")
    if parsed.port is None or parsed.port <= 0 or parsed.port > 65535:
        raise ContractError("OPC UA endpoint must include a valid non-zero port")
    return endpoint


def _require_loopback_http(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ContractError("preview base URL must use loopback HTTP")
    if parsed.port is None or parsed.port <= 0 or parsed.port > 65535:
        raise ContractError("preview base URL must include a valid port")
    return url.rstrip("/")


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"failed to load {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must contain a YAML mapping")
    return value


def _number_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _manifest_sources(raw_manifest: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    interfaces = raw_manifest.get("interfaces")
    if not isinstance(interfaces, Mapping):
        raise ContractError("manifest.interfaces must be a mapping")
    result: dict[str, Mapping[str, Any]] = {}
    for kind in ("state", "action", "cameras"):
        entries = interfaces.get(kind)
        if not isinstance(entries, list):
            raise ContractError(f"manifest.interfaces.{kind} must be a list")
        normalized_kind = "camera" if kind == "cameras" else kind
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("key"), str):
                raise ContractError(f"manifest.interfaces.{kind} has an invalid entry")
            result[entry["key"]] = {**entry, "_kind": normalized_kind}
    return result


def _v2_engineering_units(mapping: Mapping[str, Any]) -> frozenset[str]:
    raw_units = mapping.get("engineering_units")
    if not isinstance(raw_units, Mapping) or not raw_units:
        raise ContractError("mapping engineering_units must be a non-empty mapping")
    canonical_uris = set()
    for unit, raw in raw_units.items():
        if (
            not isinstance(unit, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._/-]{0,63}", unit)
            or not isinstance(raw, Mapping)
            or set(raw) != {"canonical_uri", "display_name", "symbol"}
        ):
            raise ContractError("mapping contains an invalid engineering unit definition")
        values = (raw["canonical_uri"], raw["display_name"], raw["symbol"])
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ContractError(f"engineering unit {unit!r} fields must be non-empty")
        canonical_uri = raw["canonical_uri"].strip()
        if not canonical_uri.startswith("urn:"):
            raise ContractError(f"engineering unit {unit!r} canonical_uri must be a URN")
        if canonical_uri in canonical_uris:
            raise ContractError("engineering unit canonical_uri values must be unique")
        canonical_uris.add(canonical_uri)
    return frozenset(raw_units)


def _validate_v2_feature_contract(mapping: Mapping[str, Any]) -> None:
    raw = mapping.get("feature_contract")
    if not isinstance(raw, Mapping):
        raise ContractError("mapping feature_contract is missing")
    required_keys = {
        "finite_values_required",
        "exact_joint_name_match_required",
        "duplicate_joint_names_allowed",
        "projected_fields",
    }
    if set(raw) != required_keys:
        raise ContractError("mapping feature_contract keys are invalid")
    for field in (
        "finite_values_required",
        "exact_joint_name_match_required",
    ):
        if raw.get(field) is not True:
            raise ContractError(f"feature_contract.{field} must be true")
    if raw.get("duplicate_joint_names_allowed") is not False:
        raise ContractError("feature_contract.duplicate_joint_names_allowed must be false")
    projected = raw.get("projected_fields")
    if projected != [
        "feature_name",
        "source_key",
        "topic",
        "semantic",
        "unit",
        "value",
        "timestamp_provenance",
    ]:
        raise ContractError("feature_contract.projected_fields is invalid")


def _parse_feature_specs(
    raw_features: Any,
    *,
    expected_features: tuple[str, ...],
    start_order: int,
    source_semantic: str,
    manifest_unit: Any,
    mapping_schema_version: str,
    mapping: Mapping[str, Any],
    engineering_units: frozenset[str],
) -> tuple[FeatureSpec, ...]:
    if not isinstance(raw_features, list):
        raise ContractError("mapping topic features must be a list")
    if mapping_schema_version == "1.0.0":
        if tuple(raw_features) != expected_features:
            raise ContractError("mapping topic feature order differs from manifest")
        joint = mapping.get("joint_contract")
        if not isinstance(joint, Mapping):
            raise ContractError("mapping joint_contract is missing")
        if joint.get("opcua_data_type") != "Float":
            raise ContractError("v1 joint OPC UA data type must be Float")
        unit = joint.get("unit")
        if not isinstance(unit, str) or unit != manifest_unit:
            raise ContractError("mapping joint unit differs from manifest")
        ranges = joint.get("non_gripper_range"), joint.get("gripper_range")
        if any(not isinstance(value, list) or len(value) != 2 for value in ranges):
            raise ContractError("joint ranges must each contain two values")
        try:
            parsed_ranges = tuple(
                (float(value[0]), float(value[1])) for value in ranges
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("joint ranges must contain finite numbers") from exc
        if any(
            not math.isfinite(lower)
            or not math.isfinite(upper)
            or lower >= upper
            for lower, upper in parsed_ranges
        ):
            raise ContractError("joint ranges must be finite and increasing")
        parsed = []
        for offset, name in enumerate(expected_features):
            valid_range = (
                parsed_ranges[1] if name.endswith("_gripper") else parsed_ranges[0]
            )
            parsed.append(
                FeatureSpec(
                    name=name,
                    order=start_order + offset,
                    selector_type="joint_name",
                    selector_value=name,
                    opcua_data_type="Float",
                    engineering_unit=unit,
                    minimum=float(valid_range[0]),
                    maximum=float(valid_range[1]),
                    semantic=source_semantic,
                )
            )
        return tuple(parsed)

    if len(raw_features) != len(expected_features):
        raise ContractError("mapping topic feature count differs from manifest")
    parsed = []
    required_keys = {
        "name",
        "order",
        "selector",
        "opcua_data_type",
        "engineering_unit",
        "valid_range",
        "semantic",
    }
    for offset, (raw, expected_name) in enumerate(
        zip(raw_features, expected_features, strict=True)
    ):
        if not isinstance(raw, Mapping) or set(raw) != required_keys:
            raise ContractError("mapping contains an invalid feature entry")
        name = raw.get("name")
        order = raw.get("order")
        if name != expected_name:
            raise ContractError("mapping feature order differs from manifest")
        if (
            not isinstance(order, int)
            or isinstance(order, bool)
            or order != start_order + offset
        ):
            raise ContractError(f"feature {name!r} order is invalid")
        selector = raw.get("selector")
        if (
            not isinstance(selector, Mapping)
            or set(selector) != {"type", "value"}
            or selector.get("type") != "joint_name"
            or selector.get("value") != name
        ):
            raise ContractError(f"feature {name!r} selector is invalid")
        if raw.get("opcua_data_type") != "Float":
            raise ContractError(f"feature {name!r} OPC UA data type must be Float")
        unit = raw.get("engineering_unit")
        if (
            not isinstance(unit, str)
            or unit not in engineering_units
            or unit != manifest_unit
        ):
            raise ContractError(f"feature {name!r} engineering unit is invalid")
        valid_range = raw.get("valid_range")
        if not isinstance(valid_range, list) or len(valid_range) != 2:
            raise ContractError(f"feature {name!r} valid_range must contain two values")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in valid_range
        ):
            raise ContractError(f"feature {name!r} valid_range must be finite")
        minimum, maximum = (float(value) for value in valid_range)
        if minimum >= maximum:
            raise ContractError(f"feature {name!r} valid_range is not increasing")
        semantic = raw.get("semantic")
        if not isinstance(semantic, str) or not semantic or semantic != source_semantic:
            raise ContractError(f"feature {name!r} semantic is invalid")
        parsed.append(
            FeatureSpec(
                name=name,
                order=order,
                selector_type="joint_name",
                selector_value=name,
                opcua_data_type="Float",
                engineering_unit=unit,
                minimum=minimum,
                maximum=maximum,
                semantic=semantic,
            )
        )
    return tuple(parsed)


def _parse_mapping_sources(
    mapping: Mapping[str, Any],
    raw_manifest: Mapping[str, Any],
    manifest: RobotManifest,
    mapping_schema_version: str,
) -> tuple[SourceSpec, ...]:
    raw_sources = mapping.get("sources")
    if not isinstance(raw_sources, list):
        raise ContractError("mapping.sources must be a list")
    manifest_raw_sources = _manifest_sources(raw_manifest)
    parsed_sources: list[SourceSpec] = []
    mapped_keys: list[str] = []
    engineering_units = (
        _v2_engineering_units(mapping)
        if mapping_schema_version == "2.0.0"
        else frozenset()
    )
    if mapping_schema_version == "2.0.0":
        _validate_v2_feature_contract(mapping)

    for raw in raw_sources:
        if not isinstance(raw, Mapping):
            raise ContractError("mapping source entries must be mappings")
        key = raw.get("key")
        kind = raw.get("kind")
        if not isinstance(key, str) or kind not in {"state", "action", "camera"}:
            raise ContractError("mapping source key/kind is invalid")
        if key not in manifest_raw_sources:
            raise ContractError(f"mapping source {key!r} is absent from manifest")
        manifest_raw = manifest_raw_sources[key]
        manifest_source = next(source for source in manifest.sources if source.key == key)
        mapped_keys.append(key)

        scalar_pairs = (
            ("kind", kind, manifest_raw["_kind"]),
            ("message_type", raw.get("message_type"), manifest_raw.get("message_type")),
            ("freshness_ms", raw.get("freshness_ms"), manifest_raw.get("freshness_ms")),
            ("required", raw.get("required"), manifest_source.required),
        )
        for label, mapped_value, manifest_value in scalar_pairs:
            if mapped_value != manifest_value:
                raise ContractError(
                    f"source {key!r} {label} differs between mapping and manifest"
                )
        if not _number_equal(raw.get("minimum_rate_hz"), manifest_raw.get("minimum_rate_hz")):
            raise ContractError(
                f"source {key!r} minimum_rate_hz differs between mapping and manifest"
            )

        topics: list[TopicSpec] = []
        source_semantic = None
        if kind in {"state", "action"}:
            source_semantic = raw.get("semantic")
            if not isinstance(source_semantic, str) or not source_semantic:
                raise ContractError(f"source {key!r} semantic is invalid")
            if manifest_raw.get("dtype") != "float32":
                raise ContractError(f"source {key!r} manifest dtype is unsupported")
            if kind == "action" and raw.get("manifest_semantic") != manifest_raw.get(
                "semantics"
            ):
                raise ContractError(
                    f"source {key!r} action semantic differs from manifest"
                )
            mapped_topics = raw.get("topics")
            if (
                not isinstance(mapped_topics, list)
                or len(mapped_topics) != len(manifest_source.topics)
            ):
                raise ContractError(f"source {key!r} topics must be a list")
            next_order = 0
            for topic_index, (topic_raw, expected_topic) in enumerate(
                zip(mapped_topics, manifest_source.topics, strict=True)
            ):
                if not isinstance(topic_raw, Mapping):
                    raise ContractError(f"source {key!r} topic entry must be a mapping")
                topic = topic_raw.get("topic")
                if topic != expected_topic:
                    raise ContractError(f"source {key!r} topic order differs from manifest")
                expected_features = manifest_source.topic_features[topic_index]
                feature_specs = _parse_feature_specs(
                    topic_raw.get("features"),
                    expected_features=expected_features,
                    start_order=next_order,
                    source_semantic=source_semantic,
                    manifest_unit=manifest_raw.get("unit"),
                    mapping_schema_version=mapping_schema_version,
                    mapping=mapping,
                    engineering_units=engineering_units,
                )
                next_order += len(feature_specs)
                topics.append(
                    TopicSpec(
                        source_key=key,
                        kind=kind,
                        topic=topic,
                        message_type=str(raw["message_type"]),
                        required=bool(raw["required"]),
                        freshness_ms=int(raw["freshness_ms"]),
                        minimum_rate_hz=(
                            None
                            if raw.get("minimum_rate_hz") is None
                            else float(raw["minimum_rate_hz"])
                        ),
                        features=tuple(spec.name for spec in feature_specs),
                        feature_specs=feature_specs,
                    )
                )
            mapped_features = [feature for topic in topics for feature in topic.features]
            if mapped_features != list(manifest_raw.get("feature_names", [])):
                raise ContractError(f"source {key!r} feature order differs from manifest")
            if tuple(topic.features for topic in topics) != manifest_source.topic_features:
                raise ContractError(
                    f"source {key!r} per-topic features differ from manifest"
                )
        else:
            topic = raw.get("topic")
            if topic != manifest_raw.get("topic"):
                raise ContractError(f"camera {key!r} topic differs from manifest")
            if raw.get("encoding") != manifest_raw.get("encoding"):
                raise ContractError(f"camera {key!r} encoding differs from manifest")
            for field, mapping_field in (
                ("width", "width"),
                ("height", "height"),
                ("configured_fps", "fps"),
            ):
                if not _number_equal(raw.get(field), manifest_raw.get(mapping_field)):
                    raise ContractError(f"camera {key!r} {field} differs from manifest")
            topics.append(
                TopicSpec(
                    source_key=key,
                    kind=kind,
                    topic=str(topic),
                    message_type=str(raw["message_type"]),
                    required=bool(raw["required"]),
                    freshness_ms=int(raw["freshness_ms"]),
                    minimum_rate_hz=float(raw["minimum_rate_hz"]),
                    features=(),
                )
            )

        parsed_sources.append(
            SourceSpec(
                key=key,
                kind=kind,
                required=bool(raw["required"]),
                message_type=str(raw["message_type"]),
                freshness_ms=int(raw["freshness_ms"]),
                minimum_rate_hz=(
                    None
                    if raw.get("minimum_rate_hz") is None
                    else float(raw["minimum_rate_hz"])
                ),
                semantic=source_semantic,
                topics=tuple(topics),
                encoding=(str(raw["encoding"]) if kind == "camera" else None),
                width=(int(raw["width"]) if kind == "camera" else None),
                height=(int(raw["height"]) if kind == "camera" else None),
                configured_fps=(
                    float(raw["configured_fps"]) if kind == "camera" else None
                ),
            )
        )

    if mapped_keys != [source.key for source in manifest.sources]:
        raise ContractError("mapping source order differs from manifest")
    return tuple(parsed_sources)


def _parse_status_codes(raw: Mapping[str, Any]) -> Mapping[str, int]:
    values = raw.get("status_codes")
    if not isinstance(values, Mapping):
        raise ContractError("status-code mapping is missing status_codes")
    required = {
        "Good",
        "Uncertain",
        "UncertainLastUsableValue",
        "UncertainDataSubNormal",
        "BadDecodingError",
        "BadNoCommunication",
        "BadWaitingForInitialData",
        "BadTypeMismatch",
        "BadConfigurationError",
    }
    parsed = {}
    for name in required:
        entry = values.get(name)
        if not isinstance(entry, Mapping) or not isinstance(entry.get("value"), str):
            raise ContractError(f"status code {name!r} is missing")
        try:
            parsed[name] = int(entry["value"], 16)
        except ValueError as exc:
            raise ContractError(f"status code {name!r} is not hexadecimal") from exc
    return parsed


def _validate_node_ids(contract: GatewayContract) -> None:
    ids = contract.node_ids
    generated = [ids.agent, ids.robot, ids.sources, ids.clock]
    for field in ("model", "real_or_sim", "manifest_schema_version", "fsm_state", "phase", "sources_fresh", "rates_ok", "fault"):
        generated.append(ids.robot_field(field))
    for field in ("sync_source", "state", "offset_ms", "skew_ppm", "last_check", "quality"):
        generated.append(ids.clock_field(field))
    for source in contract.sources:
        generated.append(ids.source(source.key))
        generated.append(ids.source_field(source.key, "topics"))
        for field in ("required", "freshness_ms", "fresh", "rate_ok"):
            generated.append(ids.source_field(source.key, field))
        for topic in source.topics:
            generated.append(ids.topic(source.key, topic.topic))
            for field in ("topic", "age_ms", "rate_hz", "minimum_rate_hz", "rate_ok", "sequence", "fresh"):
                generated.append(ids.topic_field(source.key, topic.topic, field))
            for feature in topic.features:
                generated.append(ids.feature(source.key, feature))
                for field in ("feature_name", "source_key", "topic", "semantic", "unit", "value", "timestamp_provenance"):
                    generated.append(ids.feature_field(source.key, feature, field))
        if source.kind == "camera":
            for field in ("source_key", "topic", "message_type", "encoding", "width", "height", "configured_fps", "last_sequence", "stream_uri", "timestamp_provenance"):
                generated.append(ids.source_field(source.key, field))
    if len(generated) != len(set(generated)):
        raise ContractError("generated NodeIds collide")
    if any(len(node_id.encode("utf-8")) > contract.maximum_node_id_bytes for node_id in generated):
        raise ContractError("generated NodeId exceeds the contract byte limit")


def _validate_expected_coverage(
    mapping: Mapping[str, Any], sources: tuple[SourceSpec, ...]
) -> None:
    expected = mapping.get("expected_coverage")
    if not isinstance(expected, Mapping):
        raise ContractError("mapping expected_coverage is missing")
    actual = {
        "state_features": sum(
            len(topic.features)
            for source in sources
            if source.kind == "state"
            for topic in source.topics
        ),
        "action_features": sum(
            len(topic.features)
            for source in sources
            if source.kind == "action"
            for topic in source.topics
        ),
        "cameras": sum(source.kind == "camera" for source in sources),
        "sources": len(sources),
        "topics": sum(len(source.topics) for source in sources),
        "required_sources": sum(source.required for source in sources),
        "optional_sources": sum(not source.required for source in sources),
    }
    if dict(expected) != actual:
        raise ContractError(
            f"mapping expected_coverage differs from parsed coverage: {actual}"
        )


def load_gateway_contract(
    *,
    manifest_path: str | Path,
    mapping_path: str | Path,
    model_contract_path: str | Path,
    status_codes_path: str | Path,
    nodeset_path: str | Path,
    endpoint: str,
    agent_id: str | None = None,
    preview_base_url: str | None = None,
    allow_remote_endpoint: bool = False,
) -> GatewayContract:
    manifest_path = Path(manifest_path)
    mapping_path = Path(mapping_path)
    model_contract_path = Path(model_contract_path)
    status_codes_path = Path(status_codes_path)
    nodeset_path = Path(nodeset_path)
    manifest = load_manifest(manifest_path)
    raw_manifest = _load_yaml(manifest_path)
    mapping = _load_yaml(mapping_path)
    model = _load_yaml(model_contract_path)
    status = _load_yaml(status_codes_path)

    effective_agent_id = agent_id or manifest.robot.host_id
    if not AGENT_ID.fullmatch(effective_agent_id):
        raise ContractError("agent_id must be URI-safe ASCII")
    if allow_remote_endpoint:
        require_secure_endpoint(endpoint)
    else:
        require_loopback_endpoint(endpoint)
    if preview_base_url is not None:
        preview_base_url = _require_loopback_http(preview_base_url)
    if not nodeset_path.is_file():
        raise ContractError(f"NodeSet does not exist: {nodeset_path}")
    mapping_schema_version = mapping.get("schema_version")
    if mapping_schema_version not in SUPPORTED_MAPPING_SCHEMA_VERSIONS:
        raise ContractError(
            f"unsupported mapping schema_version: {mapping_schema_version!r}"
        )
    if mapping.get("profile") != EXPECTED_PROFILE or model.get("profile_id") != EXPECTED_PROFILE:
        raise ContractError("mapping/model profile mismatch")
    if model.get("status") != "frozen_for_phase_1":
        raise ContractError("model contract is not frozen for Phase 1")

    model_section = model.get("model")
    edge_section = model.get("edge")
    node_id_section = model.get("node_ids")
    timestamps = model.get("timestamps")
    live_validation = model.get("phase_1_live_validation")
    if not all(
        isinstance(value, Mapping)
        for value in (
            model_section,
            edge_section,
            node_id_section,
            timestamps,
            live_validation,
        )
    ):
        raise ContractError("model contract sections are missing")
    if model_section.get("namespace_uri") != MODEL_NAMESPACE_URI:
        raise ContractError("unexpected model namespace")
    if edge_section.get("instance_namespace_uri") != EDGE_NAMESPACE_URI:
        raise ContractError("unexpected Edge namespace")
    if edge_section.get("writable_application_variables") != 0 or edge_section.get("application_methods") != 0:
        raise ContractError("Phase 1 contract must remain read-only and method-free")

    identity = mapping.get("identity")
    if not isinstance(identity, Mapping):
        raise ContractError("mapping identity is missing")
    if identity.get("robot_id") != manifest.robot.robot_id:
        raise ContractError("mapping robot_id differs from manifest")
    if identity.get("model") != manifest.robot.model:
        raise ContractError("mapping model differs from manifest")
    if identity.get("real_or_sim") != manifest.robot.real_or_sim:
        raise ContractError("mapping real_or_sim differs from manifest")
    if identity.get("instance_namespace_uri") != EDGE_NAMESPACE_URI:
        raise ContractError("mapping Edge namespace differs from model contract")

    sources = _parse_mapping_sources(
        mapping,
        raw_manifest,
        manifest,
        str(mapping_schema_version),
    )
    _validate_expected_coverage(mapping, sources)
    minimum_matched_samples = live_validation.get(
        "minimum_matched_samples_per_active_topic"
    )
    maximum_unmatched_ratio = live_validation.get("maximum_unmatched_read_ratio")
    required_provenance = live_validation.get("required_timestamp_provenance")
    phase_2 = timestamps.get("phase_2")
    if not isinstance(phase_2, Mapping):
        raise ContractError("Phase 2 timestamp contract is missing")
    if phase_2.get("status") != "frozen":
        raise ContractError("Phase 2 timestamp contract must be frozen")
    for field in (
        "comparison_window_s",
        "startup_grace_s",
        "reconnect_grace_s",
        "heartbeat_max_gap_s",
        "minimum_strict_duration_s",
    ):
        value = phase_2.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0.0
        ):
            raise ContractError(f"Phase 2 {field} must be positive")
    authoritative_health_timeout_s = phase_2.get(
        "local_health_snapshot_timeout_s"
    )
    if (
        not isinstance(authoritative_health_timeout_s, (int, float))
        or isinstance(authoritative_health_timeout_s, bool)
        or not math.isfinite(authoritative_health_timeout_s)
        or authoritative_health_timeout_s <= 0.0
    ):
        raise ContractError("local health snapshot timeout must be positive")
    if not isinstance(minimum_matched_samples, int) or minimum_matched_samples <= 0:
        raise ContractError("live validation minimum matched samples must be positive")
    if (
        not isinstance(maximum_unmatched_ratio, (int, float))
        or isinstance(maximum_unmatched_ratio, bool)
        or not 0.0 <= float(maximum_unmatched_ratio) <= 1.0
    ):
        raise ContractError("live validation unmatched ratio must be between 0 and 1")
    if not isinstance(required_provenance, str) or not required_provenance:
        raise ContractError("live validation timestamp provenance is missing")
    contract = GatewayContract(
        agent_id=effective_agent_id,
        application_uri=f"urn:sang:agent:{effective_agent_id}",
        endpoint=endpoint,
        manifest=manifest,
        mapping_schema_version=str(mapping_schema_version),
        mapping_digest=(
            mapping_health_digest(mapping)
            if mapping_schema_version == "2.0.0"
            else None
        ),
        model_namespace_uri=MODEL_NAMESPACE_URI,
        edge_namespace_uri=EDGE_NAMESPACE_URI,
        nodeset_path=nodeset_path,
        sources=sources,
        status_codes=_parse_status_codes(status),
        startup_grace_s=float(timestamps["edge_quality_timing"]["initial_data_grace_s"]),
        authoritative_health_timeout_s=float(authoritative_health_timeout_s),
        minimum_matched_samples=minimum_matched_samples,
        maximum_unmatched_ratio=float(maximum_unmatched_ratio),
        required_timestamp_provenance=required_provenance,
        preview_base_url=preview_base_url,
        maximum_node_id_bytes=int(node_id_section["maximum_identifier_utf8_bytes"]),
    )
    _validate_node_ids(contract)
    return contract
