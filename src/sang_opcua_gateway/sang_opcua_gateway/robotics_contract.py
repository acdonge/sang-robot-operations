"""Versioned identity and mapping contracts for OPC UA Robotics projection."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,191}$")
SUPPORT_STATES = frozenset({"unsupported", "partial", "base_candidate", "validated"})
COVERAGE_STATES = frozenset({"available", "missing", "not_applicable"})
MANDATORY_COVERAGE = frozenset(
    {
        "motion_device_system",
        "identification",
        "motion_devices",
        "controllers",
        "axes",
        "power_trains",
        "safety_states",
        "actual_position",
    }
)


class RoboticsContractError(ValueError):
    """Raised when a Robotics profile could overstate available evidence."""


@dataclass(frozen=True)
class RoboticsIdentities:
    processing_node_id: str
    cell_agent_id: str
    runtime_agent_id: str
    asset_id: str
    motion_device_system_id: str


@dataclass(frozen=True)
class CoverageEntry:
    element: str
    mandatory: bool
    state: str
    source: str | None
    opcua_type: str | None
    quality_policy: str | None
    update_rate_hz: float | None
    provenance: str | None


@dataclass(frozen=True)
class AxisMapping:
    axis_id: str
    feature_name: str
    source_key: str
    engineering_unit: str
    minimum: float
    maximum: float
    minimum_rate_hz: float
    power_train_id: str


@dataclass(frozen=True)
class MotionDeviceProfile:
    motion_device_id: str
    display_name: str
    axes: tuple[AxisMapping, ...]
    power_train_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoboticsProfile:
    profile_id: str
    robot_id: str
    runtime_kind: str
    support_status: str
    conformance_claim: bool
    identities: RoboticsIdentities
    manufacturer: str | None
    model: str | None
    product_code: str | None
    serial_number: str | None
    controller: Mapping[str, str] | None
    motion_devices: tuple[MotionDeviceProfile, ...]
    safety_states: tuple[Mapping[str, str], ...]
    coverage: Mapping[str, CoverageEntry]
    evidence_refs: tuple[str, ...]

    @property
    def axes(self) -> tuple[AxisMapping, ...]:
        return tuple(axis for device in self.motion_devices for axis in device.axes)


@dataclass(frozen=True)
class RoboticsProfileCatalog:
    schema_version: str
    official_models: Mapping[str, Mapping[str, str]]
    profiles: tuple[RoboticsProfile, ...]

    def for_robot(self, robot_id: str) -> RoboticsProfile:
        for profile in self.profiles:
            if profile.robot_id == robot_id:
                return profile
        raise KeyError(robot_id)

    def for_profile(self, profile_id: str) -> RoboticsProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(profile_id)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RoboticsContractError(f"failed to load {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RoboticsContractError(f"{path} must contain a mapping")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RoboticsContractError(f"{label} must be a mapping")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise RoboticsContractError(f"{label} is not a valid stable identifier")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RoboticsContractError(f"{label} must be a non-empty string or null")
    return value.strip()


def _positive_number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RoboticsContractError(f"{label} must be a positive finite number")
    return float(value)


def _parse_coverage(raw: Any, support_status: str) -> Mapping[str, CoverageEntry]:
    if not isinstance(raw, list):
        raise RoboticsContractError("profile coverage must be a list")
    parsed: dict[str, CoverageEntry] = {}
    required_keys = {
        "element",
        "mandatory",
        "state",
        "source",
        "opcua_type",
        "quality_policy",
        "update_rate_hz",
        "provenance",
    }
    for item in raw:
        entry = _mapping(item, "coverage entry")
        if set(entry) != required_keys:
            raise RoboticsContractError("coverage entry keys are invalid")
        element = _safe_id(entry["element"], "coverage element")
        if element in parsed:
            raise RoboticsContractError(f"duplicate coverage element: {element}")
        state = entry["state"]
        if state not in COVERAGE_STATES:
            raise RoboticsContractError(f"invalid coverage state for {element}")
        mandatory = entry["mandatory"]
        if not isinstance(mandatory, bool):
            raise RoboticsContractError(f"coverage mandatory flag is invalid for {element}")
        update_rate = entry["update_rate_hz"]
        parsed[element] = CoverageEntry(
            element=element,
            mandatory=mandatory,
            state=state,
            source=_optional_text(entry["source"], f"{element}.source"),
            opcua_type=_optional_text(entry["opcua_type"], f"{element}.opcua_type"),
            quality_policy=_optional_text(
                entry["quality_policy"], f"{element}.quality_policy"
            ),
            update_rate_hz=(
                None
                if update_rate is None
                else _positive_number(update_rate, f"{element}.update_rate_hz")
            ),
            provenance=_optional_text(entry["provenance"], f"{element}.provenance"),
        )
    missing_entries = MANDATORY_COVERAGE - set(parsed)
    if missing_entries:
        raise RoboticsContractError(
            "coverage is missing mandatory matrix rows: "
            + ", ".join(sorted(missing_entries))
        )
    missing_values = {
        key for key in MANDATORY_COVERAGE if parsed[key].state == "missing"
    }
    if support_status in {"base_candidate", "validated"} and missing_values:
        raise RoboticsContractError(
            f"{support_status} profile has missing mandatory coverage: "
            + ", ".join(sorted(missing_values))
        )
    if support_status == "partial" and not missing_values:
        raise RoboticsContractError("partial profile must identify missing mandatory coverage")
    return parsed


def _parse_axes(raw: Any, power_train_ids: set[str]) -> tuple[AxisMapping, ...]:
    if not isinstance(raw, list):
        raise RoboticsContractError("motion device axes must be a list")
    axes = []
    for item in raw:
        axis = _mapping(item, "axis mapping")
        axis_id = _safe_id(axis.get("axis_id"), "axis_id")
        feature_name = _safe_id(axis.get("feature_name"), "feature_name")
        source_key = _safe_id(axis.get("source_key"), "source_key")
        unit = _safe_id(axis.get("engineering_unit"), "engineering_unit")
        valid_range = axis.get("valid_range")
        if not isinstance(valid_range, list) or len(valid_range) != 2:
            raise RoboticsContractError(f"axis {axis_id} valid_range is invalid")
        try:
            minimum, maximum = (float(value) for value in valid_range)
        except (TypeError, ValueError) as exc:
            raise RoboticsContractError(f"axis {axis_id} range is not numeric") from exc
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
            raise RoboticsContractError(f"axis {axis_id} range must be finite and increasing")
        power_train_id = _safe_id(axis.get("power_train_id"), "power_train_id")
        if power_train_id not in power_train_ids:
            raise RoboticsContractError(
                f"axis {axis_id} references unknown power train {power_train_id}"
            )
        axes.append(
            AxisMapping(
                axis_id=axis_id,
                feature_name=feature_name,
                source_key=source_key,
                engineering_unit=unit,
                minimum=minimum,
                maximum=maximum,
                minimum_rate_hz=_positive_number(
                    axis.get("minimum_rate_hz"), f"axis {axis_id} minimum_rate_hz"
                ),
                power_train_id=power_train_id,
            )
        )
    identities = [axis.axis_id for axis in axes]
    features = [(axis.source_key, axis.feature_name) for axis in axes]
    if len(identities) != len(set(identities)) or len(features) != len(set(features)):
        raise RoboticsContractError("axis IDs and source feature mappings must be unique")
    return tuple(axes)


def _parse_motion_devices(raw: Any) -> tuple[MotionDeviceProfile, ...]:
    if not isinstance(raw, list):
        raise RoboticsContractError("profile motion_devices must be a list")
    devices = []
    for item in raw:
        device = _mapping(item, "motion device")
        power_trains_raw = device.get("power_trains")
        if not isinstance(power_trains_raw, list):
            raise RoboticsContractError("motion device power_trains must be a list")
        power_train_ids = {
            _safe_id(power_train.get("power_train_id"), "power_train_id")
            for power_train in (
                _mapping(entry, "power train") for entry in power_trains_raw
            )
        }
        if len(power_train_ids) != len(power_trains_raw):
            raise RoboticsContractError("power train IDs must be unique")
        devices.append(
            MotionDeviceProfile(
                motion_device_id=_safe_id(
                    device.get("motion_device_id"), "motion_device_id"
                ),
                display_name=_optional_text(
                    device.get("display_name"), "motion device display_name"
                )
                or "",
                axes=_parse_axes(device.get("axes"), power_train_ids),
                power_train_ids=tuple(sorted(power_train_ids)),
            )
        )
    device_ids = [device.motion_device_id for device in devices]
    if len(device_ids) != len(set(device_ids)):
        raise RoboticsContractError("motion device IDs must be unique")
    return tuple(devices)


def _parse_profile(raw: Any) -> RoboticsProfile:
    profile = _mapping(raw, "Robotics profile")
    support_status = profile.get("support_status")
    if support_status not in SUPPORT_STATES:
        raise RoboticsContractError("profile support_status is invalid")
    conformance_claim = profile.get("conformance_claim")
    if not isinstance(conformance_claim, bool):
        raise RoboticsContractError("profile conformance_claim must be Boolean")
    evidence = profile.get("evidence_refs")
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise RoboticsContractError("profile evidence_refs must contain strings")
    if conformance_claim and (
        support_status != "validated"
        or not any("ctt" in item.casefold() for item in evidence)
    ):
        raise RoboticsContractError(
            "conformance claims require validated status and CTT evidence"
        )
    identities_raw = _mapping(profile.get("identities"), "profile identities")
    identities = RoboticsIdentities(
        processing_node_id=_safe_id(
            identities_raw.get("processing_node_id"), "processing_node_id"
        ),
        cell_agent_id=_safe_id(
            identities_raw.get("cell_agent_id"), "cell_agent_id"
        ),
        runtime_agent_id=_safe_id(
            identities_raw.get("runtime_agent_id"), "runtime_agent_id"
        ),
        asset_id=_safe_id(identities_raw.get("asset_id"), "asset_id"),
        motion_device_system_id=_safe_id(
            identities_raw.get("motion_device_system_id"),
            "motion_device_system_id",
        ),
    )
    if len(set(identities.__dict__.values())) != len(identities.__dict__):
        raise RoboticsContractError("profile identity roles must not share one identifier")

    controller_raw = profile.get("controller")
    controller = None
    if controller_raw is not None:
        parsed_controller = _mapping(controller_raw, "controller")
        controller = {
            key: _optional_text(parsed_controller.get(key), f"controller.{key}") or ""
            for key in ("controller_id", "manufacturer", "model", "product_code", "serial_number")
        }
        if any(not value for value in controller.values()):
            raise RoboticsContractError("controller fields must all be present when supplied")

    safety_states_raw = profile.get("safety_states")
    if not isinstance(safety_states_raw, list):
        raise RoboticsContractError("profile safety_states must be a list")
    safety_states = tuple(
        {
            "safety_state_id": _safe_id(
                _mapping(item, "safety state").get("safety_state_id"),
                "safety_state_id",
            ),
            "source": _optional_text(
                _mapping(item, "safety state").get("source"), "safety state source"
            )
            or "",
        }
        for item in safety_states_raw
    )
    if any(not item["source"] for item in safety_states):
        raise RoboticsContractError("safety state source must be explicit")

    parsed = RoboticsProfile(
        profile_id=_safe_id(profile.get("profile_id"), "profile_id"),
        robot_id=_safe_id(profile.get("robot_id"), "robot_id"),
        runtime_kind=_safe_id(profile.get("runtime_kind"), "runtime_kind"),
        support_status=support_status,
        conformance_claim=conformance_claim,
        identities=identities,
        manufacturer=_optional_text(profile.get("manufacturer"), "manufacturer"),
        model=_optional_text(profile.get("model"), "model"),
        product_code=_optional_text(profile.get("product_code"), "product_code"),
        serial_number=_optional_text(profile.get("serial_number"), "serial_number"),
        controller=controller,
        motion_devices=_parse_motion_devices(profile.get("motion_devices")),
        safety_states=safety_states,
        coverage=_parse_coverage(profile.get("coverage"), support_status),
        evidence_refs=tuple(evidence),
    )
    if support_status in {"base_candidate", "validated"}:
        required_identity = (
            parsed.manufacturer,
            parsed.model,
            parsed.product_code,
            parsed.serial_number,
            parsed.controller,
            parsed.motion_devices,
            parsed.safety_states,
        )
        if any(not value for value in required_identity) or any(
            not device.axes or not device.power_train_ids
            for device in parsed.motion_devices
        ):
            raise RoboticsContractError(
                f"{support_status} profile lacks truthful mandatory instance data"
            )
    return parsed


def load_robotics_profile_catalog(path: str | Path) -> RoboticsProfileCatalog:
    path = Path(path)
    raw = _load_yaml(path)
    if raw.get("schema_version") != "1.0.0":
        raise RoboticsContractError("unsupported Robotics profile schema_version")
    official_models = _mapping(raw.get("official_models"), "official_models")
    required_models = {"core", "di", "ia", "robotics"}
    if set(official_models) != required_models:
        raise RoboticsContractError("official model dependency set is incomplete")
    for name, model in official_models.items():
        definition = _mapping(model, f"official model {name}")
        if not all(
            isinstance(definition.get(field), str) and definition[field]
            for field in ("uri", "version", "path", "sha256")
        ):
            raise RoboticsContractError(f"official model {name} is incomplete")
    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise RoboticsContractError("Robotics profile catalog must not be empty")
    profiles = tuple(_parse_profile(item) for item in profiles_raw)
    for label, values in (
        ("profile_id", [profile.profile_id for profile in profiles]),
        ("robot_id", [profile.robot_id for profile in profiles]),
        ("runtime_agent_id", [profile.identities.runtime_agent_id for profile in profiles]),
        ("asset_id", [profile.identities.asset_id for profile in profiles]),
        (
            "motion_device_system_id",
            [profile.identities.motion_device_system_id for profile in profiles],
        ),
    ):
        if len(values) != len(set(values)):
            raise RoboticsContractError(f"catalog {label} values must be unique")
    return RoboticsProfileCatalog(
        schema_version="1.0.0",
        official_models={key: dict(value) for key, value in official_models.items()},
        profiles=profiles,
    )
