"""Loopback-only OPC UA Server that projects the latest gateway state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import threading
import time
from typing import Hashable

from asyncua import Server, ua

from sang_security.opcua import OpcUaServerSecurity, create_secure_server

from sang_opcua_gateway.contract import FeatureSpec, GatewayContract
from sang_opcua_gateway.official_nodesets import (
    OfficialNodeSetBundle,
    import_official_nodesets,
)
from sang_opcua_gateway.robotics_contract import RoboticsProfile
from sang_opcua_gateway.robotics_projection import RoboticsAddressSpace
from sang_opcua_gateway.state import GatewayStore, RobotSnapshot, ValueSnapshot


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class _Binding:
    node_id: ua.NodeId
    variant_type: ua.VariantType


@dataclass(frozen=True)
class RoboticsProjectionConfig:
    bundle: OfficialNodeSetBundle
    profile: RoboticsProfile


class AddressSpace:
    """Build exact v1 instance NodeIds and update only server-side values."""

    def __init__(self, server: Server, contract: GatewayContract) -> None:
        self.server = server
        self.contract = contract
        self.model_ns = 0
        self.edge_ns = 0
        self._bindings: dict[Hashable, _Binding] = {}
        self._last_written: dict[Hashable, tuple[object, str, datetime | None]] = {}

    def _node_id(self, identifier: str) -> ua.NodeId:
        return ua.NodeId(identifier, self.edge_ns)

    def _browse_name(self, name: str, *, model: bool = False) -> ua.QualifiedName:
        return ua.QualifiedName(name, self.model_ns if model else self.edge_ns)

    def _type_id(self, name: str) -> ua.NodeId:
        return ua.NodeId(f"type/{name}", self.model_ns)

    async def _add_object(
        self,
        parent,
        identifier: str,
        browse_name: str,
        *,
        type_name: str | None = None,
        model_browse_name: bool = False,
    ):
        node = await parent.add_object(
            self._node_id(identifier),
            self._browse_name(browse_name, model=model_browse_name),
        )
        if type_name is not None:
            await node.delete_reference(
                ua.NodeId(ua.ObjectIds.BaseObjectType),
                ua.NodeId(ua.ObjectIds.HasTypeDefinition),
            )
            await node.add_reference(
                self._type_id(type_name),
                ua.NodeId(ua.ObjectIds.HasTypeDefinition),
            )
        return node

    async def _add_field(
        self,
        parent,
        identifier: str,
        browse_name: str,
        value,
        variant_type: ua.VariantType,
        *,
        binding: Hashable | None = None,
        property_field: bool = False,
        minimum_sampling_ms: float | None = None,
    ):
        add = parent.add_property if property_field else parent.add_variable
        node = await add(
            self._node_id(identifier),
            self._browse_name(browse_name, model=True),
            value,
            variant_type,
        )
        if binding is not None:
            self._bindings[binding] = _Binding(node.nodeid, variant_type)
        if minimum_sampling_ms is not None:
            await node.write_attribute(
                ua.AttributeIds.MinimumSamplingInterval,
                ua.DataValue(ua.Variant(minimum_sampling_ms, ua.VariantType.Double)),
            )
        return node

    async def build(self) -> None:
        self.model_ns = await self.server.get_namespace_index(
            self.contract.model_namespace_uri
        )
        self.edge_ns = await self.server.register_namespace(
            self.contract.edge_namespace_uri
        )
        ids = self.contract.node_ids
        agent = await self._add_object(
            self.server.nodes.objects,
            ids.agent,
            self.contract.agent_id,
        )
        robot = await self._add_object(
            agent,
            ids.robot,
            self.contract.manifest.robot.robot_id,
            type_name="SangRobotObservationType",
        )
        await self._build_robot(robot)

    async def _build_robot(self, robot) -> None:
        ids = self.contract.node_ids
        manifest = self.contract.manifest
        for field, browse, value in (
            ("model", "Model", manifest.robot.model),
            ("real_or_sim", "RealOrSim", manifest.robot.real_or_sim),
            ("manifest_schema_version", "ManifestSchemaVersion", manifest.schema_version),
        ):
            await self._add_field(
                robot,
                ids.robot_field(field),
                browse,
                value,
                ua.VariantType.String,
                property_field=True,
            )
        for field, browse, initial, variant in (
            ("fsm_state", "FsmState", "STARTING", ua.VariantType.String),
            ("phase", "Phase", "MONITORING", ua.VariantType.String),
            ("sources_fresh", "SourcesFresh", False, ua.VariantType.Boolean),
            ("rates_ok", "RatesOk", True, ua.VariantType.Boolean),
            ("fault", "Fault", "", ua.VariantType.String),
        ):
            await self._add_field(
                robot,
                ids.robot_field(field),
                browse,
                initial,
                variant,
                binding=("robot", field),
                minimum_sampling_ms=1000.0,
            )

        await self._build_clock(robot)
        sources_parent = await self._add_object(
            robot,
            ids.sources,
            "Sources",
            model_browse_name=True,
        )
        for source in self.contract.sources:
            source_type = (
                "SangCameraSourceType"
                if source.kind == "camera"
                else "SangSourceHealthType"
            )
            source_node = await self._add_object(
                sources_parent,
                ids.source(source.key),
                source.key,
                type_name=source_type,
            )
            await self._build_source(source_node, source)

    async def _build_clock(self, robot) -> None:
        ids = self.contract.node_ids
        clock = await self._add_object(
            robot,
            ids.clock,
            "Clock",
            type_name="SangClockStatusType",
            model_browse_name=True,
        )
        defaults = (
            ("sync_source", "SyncSource", "local_system_clock", ua.VariantType.String),
            ("state", "State", "unsupported", ua.VariantType.String),
            ("offset_ms", "OffsetMs", 0.0, ua.VariantType.Double),
            ("skew_ppm", "SkewPpm", 0.0, ua.VariantType.Double),
            ("last_check", "LastCheck", datetime(1970, 1, 1, tzinfo=UTC), ua.VariantType.DateTime),
            ("quality", "Quality", "local_only", ua.VariantType.String),
        )
        for field, browse, value, variant in defaults:
            await self._add_field(
                clock,
                ids.clock_field(field),
                browse,
                value,
                variant,
                binding=("clock", field),
                minimum_sampling_ms=1000.0,
            )

    async def _build_source(self, source_node, source) -> None:
        ids = self.contract.node_ids
        for field, browse, value, variant in (
            ("required", "Required", source.required, ua.VariantType.Boolean),
            ("freshness_ms", "FreshnessMs", source.freshness_ms, ua.VariantType.UInt32),
        ):
            await self._add_field(
                source_node,
                ids.source_field(source.key, field),
                browse,
                value,
                variant,
                property_field=True,
            )
        for field, browse, initial, variant in (
            ("fresh", "Fresh", False, ua.VariantType.Boolean),
            ("rate_ok", "RateOk", False, ua.VariantType.Boolean),
        ):
            await self._add_field(
                source_node,
                ids.source_field(source.key, field),
                browse,
                initial,
                variant,
                binding=("source", source.key, field),
                minimum_sampling_ms=500.0,
            )

        if source.kind == "camera":
            camera_topic = source.topics[0]
            for field, browse, value, variant in (
                ("source_key", "SourceKey", source.key, ua.VariantType.String),
                ("topic", "Topic", camera_topic.topic, ua.VariantType.String),
                ("message_type", "MessageType", source.message_type, ua.VariantType.String),
                ("encoding", "Encoding", source.encoding, ua.VariantType.String),
                ("width", "Width", source.width, ua.VariantType.UInt32),
                ("height", "Height", source.height, ua.VariantType.UInt32),
                ("configured_fps", "ConfiguredFps", source.configured_fps, ua.VariantType.Double),
            ):
                await self._add_field(
                    source_node,
                    ids.source_field(source.key, field),
                    browse,
                    value,
                    variant,
                    property_field=True,
                )
            for field, browse, initial, variant in (
                ("last_sequence", "LastSequence", 0, ua.VariantType.UInt64),
                ("stream_uri", "StreamUri", self.contract.stream_uri(source.key), ua.VariantType.String),
                ("timestamp_provenance", "TimestampProvenance", "", ua.VariantType.String),
            ):
                await self._add_field(
                    source_node,
                    ids.source_field(source.key, field),
                    browse,
                    initial,
                    variant,
                    binding=("camera", source.key, field),
                    minimum_sampling_ms=500.0,
                )

        topics_parent = await self._add_object(
            source_node,
            ids.source_field(source.key, "topics"),
            "Topics",
            model_browse_name=True,
        )
        for topic in source.topics:
            topic_node = await self._add_object(
                topics_parent,
                ids.topic(source.key, topic.topic),
                topic.topic,
                type_name="SangTopicHealthType",
            )
            await self._build_topic(topic_node, source, topic)
            for feature in topic.feature_specs:
                feature_node = await self._add_object(
                    source_node,
                    ids.feature(source.key, feature.name),
                    feature.name,
                    type_name="SangJointFeatureType",
                )
                await self._build_feature(feature_node, source, topic, feature)

    async def _build_topic(self, topic_node, source, topic) -> None:
        ids = self.contract.node_ids
        for field, browse, value, variant in (
            ("topic", "Topic", topic.topic, ua.VariantType.String),
            ("minimum_rate_hz", "MinimumRateHz", topic.minimum_rate_hz or 0.0, ua.VariantType.Double),
        ):
            await self._add_field(
                topic_node,
                ids.topic_field(source.key, topic.topic, field),
                browse,
                value,
                variant,
                property_field=True,
            )
        for field, browse, initial, variant in (
            ("age_ms", "AgeMs", 0.0, ua.VariantType.Double),
            ("rate_hz", "RateHz", 0.0, ua.VariantType.Double),
            ("rate_ok", "RateOk", False, ua.VariantType.Boolean),
            ("sequence", "Sequence", 0, ua.VariantType.UInt64),
            ("fresh", "Fresh", False, ua.VariantType.Boolean),
        ):
            await self._add_field(
                topic_node,
                ids.topic_field(source.key, topic.topic, field),
                browse,
                initial,
                variant,
                binding=("topic", source.key, topic.topic, field),
                minimum_sampling_ms=500.0,
            )

    async def _build_feature(
        self,
        feature_node,
        source,
        topic,
        feature: FeatureSpec,
    ) -> None:
        ids = self.contract.node_ids
        for field, browse, value in (
            ("feature_name", "FeatureName", feature.name),
            ("source_key", "SourceKey", source.key),
            ("topic", "Topic", topic.topic),
            ("semantic", "Semantic", feature.semantic),
            ("unit", "Unit", feature.engineering_unit),
        ):
            await self._add_field(
                feature_node,
                ids.feature_field(source.key, feature.name, field),
                browse,
                value,
                ua.VariantType.String,
                property_field=True,
            )
        await self._add_field(
            feature_node,
            ids.feature_field(source.key, feature.name, "value"),
            "Value",
            0.0,
            ua.VariantType.Float,
            binding=("feature", source.key, feature.name, "value"),
            minimum_sampling_ms=50.0,
        )
        await self._add_field(
            feature_node,
            ids.feature_field(source.key, feature.name, "timestamp_provenance"),
            "TimestampProvenance",
            "",
            ua.VariantType.String,
            binding=("feature", source.key, feature.name, "timestamp_provenance"),
            minimum_sampling_ms=50.0,
        )

    async def _write(
        self,
        key: Hashable,
        value,
        *,
        status: str = "Good",
        source_timestamp: datetime | None = None,
        server_timestamp: datetime,
        track_source_timestamp: bool = True,
    ) -> None:
        binding = self._bindings[key]
        timestamp_identity = source_timestamp if track_source_timestamp else None
        identity = (value, status, timestamp_identity)
        if self._last_written.get(key) == identity:
            return
        if value is None:
            value = self._missing_sentinel(binding.variant_type)
        variant = ua.Variant(value, binding.variant_type)
        data_value = ua.DataValue(
            Value=variant,
            StatusCode=ua.StatusCode(self.contract.status_codes[status]),
            SourceTimestamp=source_timestamp,
            ServerTimestamp=server_timestamp,
        )
        await self.server.write_attribute_value(binding.node_id, data_value)
        self._last_written[key] = identity

    @staticmethod
    def _missing_sentinel(variant_type: ua.VariantType):
        if variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
            return math.nan
        if variant_type == ua.VariantType.Boolean:
            return False
        if variant_type in {
            ua.VariantType.Byte,
            ua.VariantType.UInt16,
            ua.VariantType.UInt32,
            ua.VariantType.UInt64,
        }:
            return 0
        if variant_type == ua.VariantType.DateTime:
            return datetime(1970, 1, 1, tzinfo=UTC)
        return ""

    async def _write_sample(
        self,
        key: Hashable,
        sample: ValueSnapshot,
        *,
        value,
        server_timestamp: datetime,
    ) -> None:
        await self._write(
            key,
            value,
            status=sample.status,
            source_timestamp=sample.source_timestamp,
            server_timestamp=server_timestamp,
        )

    async def publish(self, snapshot: RobotSnapshot, *, include_health: bool) -> None:
        now = datetime.now(UTC)
        evaluator_status = (
            "Good" if snapshot.health_evaluator_available else "BadNoCommunication"
        )

        def health_status(value) -> str:
            if not snapshot.health_evaluator_available:
                return "BadNoCommunication"
            return "Good" if value is not None else "BadWaitingForInitialData"

        if include_health:
            for field, value in (
                ("fsm_state", snapshot.fsm_state),
                ("phase", snapshot.phase),
                ("sources_fresh", snapshot.sources_fresh),
                ("rates_ok", snapshot.rates_ok),
                ("fault", snapshot.fault),
            ):
                await self._write(
                    ("robot", field),
                    value,
                    status=evaluator_status,
                    source_timestamp=snapshot.evaluated_at,
                    server_timestamp=now,
                )

            for field, value, status in (
                ("sync_source", "local_system_clock", "Good"),
                ("state", "unsupported", "Good"),
                ("offset_ms", None, "Uncertain"),
                ("skew_ppm", None, "Uncertain"),
                ("last_check", None, "Uncertain"),
                ("quality", "local_only", "Uncertain"),
            ):
                await self._write(
                    ("clock", field),
                    value,
                    status=status,
                    source_timestamp=snapshot.evaluated_at,
                    server_timestamp=now,
                )

        for source in snapshot.sources.values():
            if include_health:
                await self._write(
                    ("source", source.key, "fresh"),
                    source.fresh,
                    status=evaluator_status,
                    source_timestamp=snapshot.evaluated_at,
                    server_timestamp=now,
                )
                await self._write(
                    ("source", source.key, "rate_ok"),
                    source.rate_ok,
                    status=health_status(source.rate_ok),
                    source_timestamp=snapshot.evaluated_at,
                    server_timestamp=now,
                )
            for topic in source.topics.values():
                if include_health:
                    for field, value in (
                        ("age_ms", topic.age_ms),
                        ("rate_hz", topic.rate_hz),
                        ("rate_ok", topic.rate_ok),
                        ("sequence", topic.sequence),
                        ("fresh", topic.fresh),
                    ):
                        await self._write(
                            ("topic", source.key, topic.topic, field),
                            value,
                            status=health_status(value),
                            source_timestamp=snapshot.evaluated_at,
                            server_timestamp=now,
                        )
                for feature, sample in topic.features.items():
                    await self._write_sample(
                        ("feature", source.key, feature, "value"),
                        sample,
                        value=sample.value,
                        server_timestamp=now,
                    )
                    await self._write_sample(
                        ("feature", source.key, feature, "timestamp_provenance"),
                        sample,
                        value=sample.provenance,
                        server_timestamp=now,
                    )
                if source.kind == "camera" and include_health:
                    await self._write_sample(
                        ("camera", source.key, "last_sequence"),
                        topic.sample,
                        value=topic.sample.value,
                        server_timestamp=now,
                    )
                    await self._write_sample(
                        ("camera", source.key, "timestamp_provenance"),
                        topic.sample,
                        value=topic.sample.provenance,
                        server_timestamp=now,
                    )
                    await self._write(
                        ("camera", source.key, "stream_uri"),
                        self.contract.stream_uri(source.key),
                        source_timestamp=snapshot.evaluated_at,
                        server_timestamp=now,
                        track_source_timestamp=False,
                    )


class OpcUaServerThread:
    """Own asyncua in one thread so ROS callbacks never block on OPC UA work."""

    def __init__(
        self,
        contract: GatewayContract,
        store: GatewayStore,
        *,
        publish_interval_s: float = 0.05,
        security: OpcUaServerSecurity | None = None,
        trust_reload_interval_s: float = 5.0,
        robotics_projection: RoboticsProjectionConfig | None = None,
    ) -> None:
        if not math.isfinite(trust_reload_interval_s) or trust_reload_interval_s <= 0.0:
            raise ValueError("trust_reload_interval_s must be positive")
        self.contract = contract
        self.store = store
        self.publish_interval_s = publish_interval_s
        self.security = security
        self.trust_reload_interval_s = trust_reload_interval_s
        self.robotics_projection = robotics_projection
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self, *, timeout_s: float = 20.0) -> None:
        if self._thread is not None:
            raise RuntimeError("OPC UA server is already started")
        self._thread = threading.Thread(
            target=self._run,
            name="sang-opcua-server",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_s):
            self.stop()
            raise TimeoutError("OPC UA server did not start before timeout")
        if self._error is not None:
            raise RuntimeError("OPC UA server failed to start") from self._error

    def stop(self, *, timeout_s: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise TimeoutError("OPC UA server did not stop before timeout")
            self._thread = None

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("OPC UA server thread failed") from self._error
        if (
            self._thread is not None
            and not self._thread.is_alive()
            and not self._stop.is_set()
        ):
            raise RuntimeError("OPC UA server thread stopped unexpectedly")

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._error = exc
            LOGGER.exception("OPC UA server stopped with an error")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        if self.security is None:
            server = Server()
            await server.init()
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
            server.set_identity_tokens([ua.AnonymousIdentityToken])
        else:
            server, trust_store = await create_secure_server(
                self.security,
                application_uri=self.contract.application_uri,
            )
        if self.security is None:
            trust_store = None
        server.set_endpoint(self.contract.endpoint)
        server.set_server_name("SANG Read-Only Robot Edge")
        await server.set_application_uri(self.contract.application_uri)
        await server.import_xml(str(self.contract.nodeset_path), strict_mode=True)
        robotics_namespaces = None
        if self.robotics_projection is not None:
            robotics_namespaces = await import_official_nodesets(
                server, self.robotics_projection.bundle
            )
        address_space = AddressSpace(server, self.contract)
        await address_space.build()
        robotics_space = None
        if self.robotics_projection is not None:
            if self.robotics_projection.profile.robot_id != self.contract.manifest.robot.robot_id:
                raise ValueError(
                    "Robotics profile robot_id does not match gateway manifest"
                )
            robotics_space = RoboticsAddressSpace(
                server,
                self.robotics_projection.profile,
                robotics_namespaces or {},
                status_codes=self.contract.status_codes,
            )
            await robotics_space.build()
        initial_snapshot = self.store.snapshot()
        await address_space.publish(initial_snapshot, include_health=True)
        if robotics_space is not None:
            await robotics_space.publish_snapshot(initial_snapshot)
        await server.start()
        self._ready.set()
        next_health = time.monotonic() + 0.5
        next_trust_reload = time.monotonic() + self.trust_reload_interval_s
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                include_health = now >= next_health
                snapshot = self.store.snapshot()
                await address_space.publish(snapshot, include_health=include_health)
                if robotics_space is not None:
                    await robotics_space.publish_snapshot(snapshot)
                if include_health:
                    next_health = now + 0.5
                if trust_store is not None and now >= next_trust_reload:
                    await trust_store.load()
                    next_trust_reload = now + self.trust_reload_interval_s
                await asyncio.sleep(self.publish_interval_s)
        finally:
            await server.stop()
