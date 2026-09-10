"""Versioned, UI-discoverable Skill parameter contracts."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, JsonValue, model_validator

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.operations_models import AccessMode, AssetType
from fleet_control_plane.orchestration_models import CapabilityRef, Sha256Digest
from fleet_control_plane.orchestration_store import payload_digest


class SkillParameterPropertyV1(FleetModel):
    type: Literal["string", "number", "integer", "boolean"]
    title: ShortText
    description: str | None = Field(default=None, max_length=1024)
    default: JsonValue | None = None
    const: JsonValue | None = None
    enum: list[JsonValue] | None = Field(default=None, min_length=1, max_length=128)
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: float | None = Field(default=None, alias="multipleOf", gt=0.0)
    unit: str | None = Field(default=None, max_length=32)
    read_only: bool = Field(default=False, alias="readOnly")

    @model_validator(mode="after")
    def constraints_are_coherent(self) -> "SkillParameterPropertyV1":
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("parameter minimum cannot exceed maximum")
        if self.type not in {"number", "integer"} and any(
            value is not None
            for value in (self.minimum, self.maximum, self.multiple_of)
        ):
            raise ValueError("only numeric parameters may define numeric bounds")
        if self.const is not None and self.enum is not None and self.const not in self.enum:
            raise ValueError("parameter const must be present in enum")
        for label, value in (("default", self.default), ("const", self.const)):
            if value is not None:
                _validate_property_value(self, value, label=label)
        for value in self.enum or []:
            _validate_property_value(self, value, label="enum")
        return self


class SkillParameterSchemaV1(FleetModel):
    type: Literal["object"] = "object"
    properties: dict[SafeId, SkillParameterPropertyV1] = Field(
        default_factory=dict,
        max_length=128,
    )
    required: list[SafeId] = Field(default_factory=list, max_length=128)
    additional_properties: Literal[False] = Field(
        default=False,
        alias="additionalProperties",
    )

    @model_validator(mode="after")
    def required_properties_exist(self) -> "SkillParameterSchemaV1":
        if len(self.required) != len(set(self.required)):
            raise ValueError("required parameter names must be unique")
        unknown = sorted(set(self.required) - set(self.properties))
        if unknown:
            raise ValueError(
                "required parameters are absent from properties: " + ", ".join(unknown)
            )
        return self


class SkillDefinitionV1(FleetModel):
    capability: CapabilityRef
    title: ShortText
    description: str = Field(min_length=1, max_length=2048)
    category: ShortText
    execution_modes: list[Literal["SIMULATION", "SHADOW", "PHYSICAL"]] = Field(
        min_length=1,
        max_length=3,
    )
    parameter_schema: SkillParameterSchemaV1

    @model_validator(mode="after")
    def execution_modes_are_unique(self) -> "SkillDefinitionV1":
        if len(self.execution_modes) != len(set(self.execution_modes)):
            raise ValueError("Skill execution modes must be unique")
        return self


class RobotActionScriptV1(FleetModel):
    runtime: Literal["UNITY_CSHARP", "ROS2_PYTHON"]
    script_ref: str = Field(
        min_length=12,
        max_length=512,
        pattern=r"^(?:unity|ros2-python)://[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    entrypoint: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
    )
    sha256: Sha256Digest

    @model_validator(mode="after")
    def script_reference_is_registry_owned(self) -> "RobotActionScriptV1":
        expected_scheme = {
            "UNITY_CSHARP": "unity://",
            "ROS2_PYTHON": "ros2-python://",
        }[self.runtime]
        if not self.script_ref.startswith(expected_scheme):
            raise ValueError(
                f"{self.runtime} action script must use {expected_scheme}"
            )
        path = self.script_ref.split("://", 1)[1]
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("action script reference contains an unsafe path segment")
        return self


class RobotActionDefinitionV1(FleetModel):
    action_ref: CapabilityRef
    robot_type: SafeId
    robot_label: ShortText
    title: ShortText
    description: str = Field(min_length=1, max_length=2048)
    execution_mode: Literal["SIMULATION", "SHADOW", "PHYSICAL"]
    capability: CapabilityRef
    role_template: SafeId
    asset_type: AssetType
    access_mode: AccessMode = "exclusive"
    step_id_template: SafeId
    fixed_parameters: dict[SafeId, JsonValue] = Field(
        default_factory=dict,
        max_length=128,
    )
    editable_parameters: list[SafeId] = Field(default_factory=list, max_length=128)
    default_timeout_s: int = Field(ge=1, le=86400)
    script: RobotActionScriptV1

    @model_validator(mode="after")
    def parameter_sets_are_disjoint(self) -> "RobotActionDefinitionV1":
        if len(self.editable_parameters) != len(set(self.editable_parameters)):
            raise ValueError("action editable parameter names must be unique")
        overlap = sorted(set(self.fixed_parameters) & set(self.editable_parameters))
        if overlap:
            raise ValueError(
                "action parameters cannot be both fixed and editable: "
                + ", ".join(overlap)
            )
        return self


class SkillCatalogV1(FleetModel):
    schema_version: Literal[1] = 1
    catalog_id: SafeId
    version: int = Field(ge=1)
    skills: list[SkillDefinitionV1] = Field(min_length=1, max_length=512)
    actions: list[RobotActionDefinitionV1] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def catalog_entries_are_coherent(self) -> "SkillCatalogV1":
        refs = [skill.capability for skill in self.skills]
        if len(refs) != len(set(refs)):
            raise ValueError("Skill capability references must be unique")
        action_refs = [action.action_ref for action in self.actions]
        if len(action_refs) != len(set(action_refs)):
            raise ValueError("Robot action references must be unique")

        definitions = {skill.capability: skill for skill in self.skills}
        step_ids: set[str] = set()
        for action in self.actions:
            definition = definitions.get(action.capability)
            if definition is None:
                raise ValueError(
                    f"action {action.action_ref} references unknown Skill "
                    f"{action.capability}"
                )
            if action.execution_mode not in definition.execution_modes:
                raise ValueError(
                    f"action {action.action_ref} execution mode is not supported "
                    f"by {action.capability}"
                )
            property_names = set(definition.parameter_schema.properties)
            action_names = set(action.fixed_parameters) | set(action.editable_parameters)
            unknown = sorted(action_names - property_names)
            if unknown:
                raise ValueError(
                    f"action {action.action_ref} references unknown parameters: "
                    + ", ".join(unknown)
                )
            missing = sorted(
                set(definition.parameter_schema.required) - action_names
            )
            if missing:
                raise ValueError(
                    f"action {action.action_ref} does not provide required parameters: "
                    + ", ".join(missing)
                )
            for name, value in action.fixed_parameters.items():
                try:
                    _validate_property_value(
                        definition.parameter_schema.properties[name],
                        value,
                        label=name,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"action {action.action_ref} fixed parameter {name}: {exc}"
                    ) from exc
            if action.step_id_template in step_ids:
                raise ValueError("Robot action step ID templates must be unique")
            step_ids.add(action.step_id_template)
        return self


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_property_value(
    definition: SkillParameterPropertyV1,
    value: JsonValue,
    *,
    label: str,
) -> None:
    if definition.type == "string" and not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if definition.type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    if definition.type == "number" and not _is_number(value):
        raise ValueError(f"{label} must be a number")
    if definition.type == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ValueError(f"{label} must be an integer")
    if definition.enum is not None and value not in definition.enum:
        raise ValueError(f"{label} must be one of {definition.enum}")
    if definition.const is not None and value != definition.const:
        raise ValueError(f"{label} must equal {definition.const!r}")
    if not _is_number(value):
        return
    numeric = float(value)
    if definition.minimum is not None and numeric < definition.minimum:
        raise ValueError(f"{label} must be >= {definition.minimum}")
    if definition.maximum is not None and numeric > definition.maximum:
        raise ValueError(f"{label} must be <= {definition.maximum}")
    if definition.multiple_of is not None:
        quotient = numeric / definition.multiple_of
        if not math.isclose(quotient, round(quotient), abs_tol=1e-8):
            raise ValueError(f"{label} must be a multiple of {definition.multiple_of}")


def validate_skill_parameters(
    definition: SkillDefinitionV1,
    parameters: dict[str, JsonValue],
) -> None:
    schema = definition.parameter_schema
    unknown = sorted(set(parameters) - set(schema.properties))
    if unknown:
        raise ValueError(
            f"{definition.capability} parameters contain unsupported fields: "
            + ", ".join(unknown)
        )
    missing = sorted(set(schema.required) - set(parameters))
    if missing:
        raise ValueError(
            f"{definition.capability} parameters require: " + ", ".join(missing)
        )
    for name, value in parameters.items():
        try:
            _validate_property_value(schema.properties[name], value, label=name)
        except ValueError as exc:
            raise ValueError(
                f"{definition.capability} parameter {name}: {exc}"
            ) from exc


def load_skill_catalog(path: str | Path) -> tuple[SkillCatalogV1, str]:
    catalog = SkillCatalogV1.model_validate_json(Path(path).read_text(encoding="utf-8"))
    payload = catalog.model_dump(mode="json", by_alias=True)
    return catalog, payload_digest(payload)
