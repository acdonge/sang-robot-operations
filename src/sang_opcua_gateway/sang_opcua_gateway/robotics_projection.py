"""Read-only OPC UA Robotics 1.02 instance projection.

The SANG observation tree remains unchanged. This module adds a parallel,
officially typed view only for profiles whose evidence is complete enough to
be a base candidate. It does not expose OPC UA Methods or command variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from asyncua import Server, ua

from sang_opcua_gateway.robotics_contract import RoboticsProfile
from sang_opcua_gateway.state import RobotSnapshot, ValueSnapshot


UTC = timezone.utc
INSTANCE_NAMESPACE_URI = "urn:sang:opcua:robotics:instances:v1"
DI_DEVICE_SET_ID = 5001
TYPE_IDS = {
    "motion_device_system": 1002,
    "controller": 1003,
    "motion_device": 1004,
    "safety_state": 1013,
    "axis": 16601,
    "power_train": 16794,
}
REFERENCE_REQUIRES = 18179
REFERENCE_HAS_SAFETY_STATES = 18182


class RoboticsProjectionError(ValueError):
    """Raised when a profile cannot be projected without inventing data."""


@dataclass(frozen=True)
class ProjectionValue:
    value: object
    status_code: int = ua.StatusCodes.Good
    source_timestamp: datetime | None = None


class RoboticsAddressSpace:
    """Build and publish the official read-only Robotics instance hierarchy."""

    def __init__(
        self,
        server: Server,
        profile: RoboticsProfile,
        namespaces: Mapping[str, int],
        *,
        status_codes: Mapping[str, int] | None = None,
    ) -> None:
        if profile.support_status not in {"base_candidate", "validated"}:
            raise RoboticsProjectionError(
                f"profile {profile.profile_id} is {profile.support_status}; "
                "official instance projection requires base_candidate or validated"
            )
        if set(namespaces) != {"di", "ia", "robotics"}:
            raise RoboticsProjectionError("official namespace set is incomplete")
        self.server = server
        self.profile = profile
        self.di_ns = namespaces["di"]
        self.robotics_ns = namespaces["robotics"]
        self.instance_ns = 0
        self.status_codes = dict(status_codes or {})
        self.mds = None
        self.controllers: dict[str, object] = {}
        self.motion_devices: dict[str, object] = {}
        self.axes: dict[str, object] = {}
        self.axis_positions: dict[str, object] = {}
        self.power_trains: dict[str, object] = {}
        self.safety_states: dict[str, object] = {}
        self.safety_values: dict[tuple[str, str], object] = {}

    def _node_id(self, *parts: str) -> ua.NodeId:
        return ua.NodeId("/".join(("robotics", *parts)), self.instance_ns)

    def _type_id(self, name: str) -> ua.NodeId:
        return ua.NodeId(TYPE_IDS[name], self.robotics_ns)

    async def _child(self, parent, name: str, namespace: int):
        return await parent.get_child(ua.QualifiedName(name, namespace))

    async def _remove_placeholders(self, parent) -> None:
        for child in await parent.get_children():
            browse_name = await child.read_browse_name()
            if browse_name.Name.startswith("<") and browse_name.Name.endswith(">"):
                await child.delete(recursive=True)

    async def _mark_tree_unknown(self, node) -> None:
        now = datetime.now(UTC)
        for child in await node.get_children():
            node_class = await child.read_node_class()
            if node_class == ua.NodeClass.Variable:
                current = await child.read_data_value(raise_on_bad_status=False)
                await self.server.write_attribute_value(
                    child.nodeid,
                    ua.DataValue(
                        Value=current.Value,
                        StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData),
                        ServerTimestamp=now,
                    ),
                )
            await self._mark_tree_unknown(child)

    async def _write(
        self,
        node,
        value: object,
        variant_type: ua.VariantType,
        *,
        status_code: int = ua.StatusCodes.Good,
        source_timestamp: datetime | None = None,
    ) -> None:
        await self.server.write_attribute_value(
            node.nodeid,
            ua.DataValue(
                Value=ua.Variant(value, variant_type),
                StatusCode=ua.StatusCode(status_code),
                SourceTimestamp=source_timestamp,
                ServerTimestamp=datetime.now(UTC),
            ),
        )

    async def _write_nameplate(self, node, values: Mapping[str, str]) -> None:
        for browse_name, key, variant_type in (
            ("Manufacturer", "manufacturer", ua.VariantType.LocalizedText),
            ("Model", "model", ua.VariantType.LocalizedText),
            ("ProductCode", "product_code", ua.VariantType.String),
            ("SerialNumber", "serial_number", ua.VariantType.String),
        ):
            value: object = values[key]
            if variant_type == ua.VariantType.LocalizedText:
                value = ua.LocalizedText(value)
            await self._write(
                await self._child(node, browse_name, self.di_ns),
                value,
                variant_type,
            )

    async def _add_standard_property(
        self,
        parent,
        identifier: str,
        browse_name: str,
        value: object,
        variant_type: ua.VariantType,
    ):
        node = await parent.add_property(
            self._node_id(identifier),
            ua.QualifiedName(browse_name, self.di_ns),
            value,
            variant_type,
        )
        return node

    async def build(self) -> None:
        self.instance_ns = await self.server.register_namespace(INSTANCE_NAMESPACE_URI)
        device_set = self.server.get_node(ua.NodeId(DI_DEVICE_SET_ID, self.di_ns))
        identity = self.profile.identities
        self.mds = await device_set.add_object(
            self._node_id(identity.motion_device_system_id),
            ua.QualifiedName(identity.motion_device_system_id, self.instance_ns),
            objecttype=self._type_id("motion_device_system"),
            instantiate_optional=False,
        )
        await self._mark_tree_unknown(self.mds)
        await self._add_standard_property(
            self.mds,
            f"{identity.motion_device_system_id}/component-name",
            "ComponentName",
            ua.LocalizedText(identity.motion_device_system_id),
            ua.VariantType.LocalizedText,
        )
        await self._build_controller()
        await self._build_motion_devices()
        await self._build_safety_states()
        await self._link_safety_states()
        await self._enforce_read_only_and_no_methods(self.mds)

    async def _build_controller(self) -> None:
        assert self.mds is not None
        controller_profile = self.profile.controller
        if controller_profile is None:
            raise RoboticsProjectionError("projectable profile has no controller")
        controllers = await self._child(self.mds, "Controllers", self.robotics_ns)
        await self._remove_placeholders(controllers)
        controller_id = controller_profile["controller_id"]
        controller = await controllers.add_object(
            self._node_id(self.profile.identities.motion_device_system_id, "controllers", controller_id),
            ua.QualifiedName(controller_id, self.instance_ns),
            objecttype=self._type_id("controller"),
            instantiate_optional=False,
        )
        await self._mark_tree_unknown(controller)
        await self._write_nameplate(controller, controller_profile)
        current_user = await self._child(controller, "CurrentUser", self.robotics_ns)
        await self._write(
            await self._child(current_user, "Level", self.robotics_ns),
            "observation-only",
            ua.VariantType.String,
        )
        for container_name in ("Software", "TaskControls"):
            await self._remove_placeholders(
                await self._child(controller, container_name, self.robotics_ns)
            )
        self.controllers[controller_id] = controller

    async def _build_motion_devices(self) -> None:
        assert self.mds is not None
        devices = await self._child(self.mds, "MotionDevices", self.robotics_ns)
        await self._remove_placeholders(devices)
        nameplate = {
            "manufacturer": self.profile.manufacturer or "",
            "model": self.profile.model or "",
            "product_code": self.profile.product_code or "",
            "serial_number": self.profile.serial_number or "",
        }
        for device_profile in self.profile.motion_devices:
            device_id = device_profile.motion_device_id
            device = await devices.add_object(
                self._node_id(self.profile.identities.motion_device_system_id, "motion-devices", device_id),
                ua.QualifiedName(device_id, self.instance_ns),
                objecttype=self._type_id("motion_device"),
                instantiate_optional=False,
            )
            await self._mark_tree_unknown(device)
            await self._write_nameplate(device, nameplate)
            await self._write(
                await self._child(device, "MotionDeviceCategory", self.robotics_ns),
                1,
                ua.VariantType.Int32,
            )
            parameter_set = await self._child(device, "ParameterSet", self.di_ns)
            await self._write(
                await self._child(parameter_set, "SpeedOverride", self.robotics_ns),
                100.0,
                ua.VariantType.Double,
            )
            self.motion_devices[device_id] = device
            await self._build_power_trains(device, device_profile)
            await self._build_axes(device, device_profile)

    async def _build_power_trains(self, device, device_profile) -> None:
        power_trains = await self._child(device, "PowerTrains", self.robotics_ns)
        await self._remove_placeholders(power_trains)
        for power_train_id in device_profile.power_train_ids:
            power_train = await power_trains.add_object(
                self._node_id(
                    self.profile.identities.motion_device_system_id,
                    "motion-devices",
                    device_profile.motion_device_id,
                    "power-trains",
                    power_train_id,
                ),
                ua.QualifiedName(power_train_id, self.instance_ns),
                objecttype=self._type_id("power_train"),
                instantiate_optional=False,
            )
            await self._mark_tree_unknown(power_train)
            await self._remove_placeholders(power_train)
            self.power_trains[power_train_id] = power_train

    async def _build_axes(self, device, device_profile) -> None:
        axes = await self._child(device, "Axes", self.robotics_ns)
        await self._remove_placeholders(axes)
        for axis_profile in device_profile.axes:
            axis = await axes.add_object(
                self._node_id(
                    self.profile.identities.motion_device_system_id,
                    "motion-devices",
                    device_profile.motion_device_id,
                    "axes",
                    axis_profile.axis_id,
                ),
                ua.QualifiedName(axis_profile.axis_id, self.instance_ns),
                objecttype=self._type_id("axis"),
                instantiate_optional=False,
            )
            await self._mark_tree_unknown(axis)
            await self._write(
                await self._child(axis, "MotionProfile", self.robotics_ns),
                1,
                ua.VariantType.Int32,
            )
            parameter_set = await self._child(axis, "ParameterSet", self.di_ns)
            actual_position = await self._child(
                parameter_set, "ActualPosition", self.robotics_ns
            )
            power_train = self.power_trains[axis_profile.power_train_id]
            await axis.add_reference(
                power_train.nodeid,
                ua.NodeId(REFERENCE_REQUIRES, self.robotics_ns),
            )
            self.axes[axis_profile.axis_id] = axis
            self.axis_positions[axis_profile.axis_id] = actual_position

    async def _build_safety_states(self) -> None:
        assert self.mds is not None
        states = await self._child(self.mds, "SafetyStates", self.robotics_ns)
        await self._remove_placeholders(states)
        for definition in self.profile.safety_states:
            safety_id = definition["safety_state_id"]
            state = await states.add_object(
                self._node_id(
                    self.profile.identities.motion_device_system_id,
                    "safety-states",
                    safety_id,
                ),
                ua.QualifiedName(safety_id, self.instance_ns),
                objecttype=self._type_id("safety_state"),
                instantiate_optional=False,
            )
            await self._mark_tree_unknown(state)
            parameter_set = await self._child(state, "ParameterSet", self.di_ns)
            for field in ("EmergencyStop", "OperationalMode", "ProtectiveStop"):
                self.safety_values[(safety_id, field)] = await self._child(
                    parameter_set, field, self.robotics_ns
                )
            self.safety_states[safety_id] = state

    async def _link_safety_states(self) -> None:
        reference_type = ua.NodeId(REFERENCE_HAS_SAFETY_STATES, self.robotics_ns)
        for controller in self.controllers.values():
            for state in self.safety_states.values():
                await controller.add_reference(state.nodeid, reference_type)

    async def _enforce_read_only_and_no_methods(self, node) -> None:
        for child in await node.get_children():
            node_class = await child.read_node_class()
            if node_class == ua.NodeClass.Method:
                raise RoboticsProjectionError(
                    f"application Method is forbidden: {child.nodeid.to_string()}"
                )
            if node_class == ua.NodeClass.Variable:
                await child.set_read_only()
            await self._enforce_read_only_and_no_methods(child)

    async def publish_axis_values(
        self, values: Mapping[str, ProjectionValue]
    ) -> None:
        unknown = set(values) - set(self.axis_positions)
        if unknown:
            raise RoboticsProjectionError(
                "axis update contains unknown IDs: " + ", ".join(sorted(unknown))
            )
        for axis_id, sample in values.items():
            await self._write(
                self.axis_positions[axis_id],
                float(sample.value),
                ua.VariantType.Double,
                status_code=sample.status_code,
                source_timestamp=sample.source_timestamp,
            )

    async def publish_safety_values(
        self,
        safety_state_id: str,
        *,
        emergency_stop: ProjectionValue,
        operational_mode: ProjectionValue,
        protective_stop: ProjectionValue,
    ) -> None:
        if safety_state_id not in self.safety_states:
            raise RoboticsProjectionError(f"unknown safety state: {safety_state_id}")
        for field, sample, variant_type in (
            ("EmergencyStop", emergency_stop, ua.VariantType.Boolean),
            ("OperationalMode", operational_mode, ua.VariantType.Int32),
            ("ProtectiveStop", protective_stop, ua.VariantType.Boolean),
        ):
            await self._write(
                self.safety_values[(safety_state_id, field)],
                sample.value,
                variant_type,
                status_code=sample.status_code,
                source_timestamp=sample.source_timestamp,
            )

    async def publish_snapshot(self, snapshot: RobotSnapshot) -> None:
        values: dict[str, ProjectionValue] = {}
        for axis in self.profile.axes:
            sample = self._find_feature(snapshot, axis.source_key, axis.feature_name)
            if sample is None:
                continue
            values[axis.axis_id] = ProjectionValue(
                value=sample.value,
                status_code=self._status_code(sample.status),
                source_timestamp=sample.source_timestamp,
            )
        if values:
            await self.publish_axis_values(values)

    def _status_code(self, name: str) -> int:
        if name in self.status_codes:
            return self.status_codes[name]
        value = getattr(ua.StatusCodes, name, None)
        return value if isinstance(value, int) else ua.StatusCodes.BadUnexpectedError

    @staticmethod
    def _find_feature(
        snapshot: RobotSnapshot, source_key: str, feature_name: str
    ) -> ValueSnapshot | None:
        source = snapshot.sources.get(source_key)
        if source is None:
            return None
        for topic in source.topics.values():
            sample = topic.features.get(feature_name)
            if sample is not None:
                return sample
        return None
