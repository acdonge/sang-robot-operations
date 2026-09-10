"""Command-free contracts for AAS identity, simulation assets, and qualification.

These models freeze Phase A0 metadata only. They do not load artifacts, open a
Unity project, connect to ROS 2, or create a physical command path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.operations_models import AssetType
from fleet_control_plane.orchestration_models import (
    CapabilityRef,
    ManifestRefV1,
    RegistryRefV1,
    RuntimeClass,
    Sha256Digest,
)


AasIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=1024,
        pattern=(
            r"^(?:urn:[A-Za-z0-9][A-Za-z0-9:._~/%-]*|"
            r"https://[A-Za-z0-9][A-Za-z0-9._~/%-]*)$"
        ),
        strip_whitespace=True,
    ),
]
ObjectId = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$", strip_whitespace=True),
]
MediaType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]+/[a-z0-9][a-z0-9!#$&^_.+-]+$",
        strip_whitespace=True,
    ),
]
VersionedContractRef = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*@[1-9][0-9]*$",
        strip_whitespace=True,
    ),
]
ArtifactFileName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+ -]*$",
        strip_whitespace=True,
    ),
]
UnityBindingId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
        strip_whitespace=True,
    ),
]

SubmodelSlot = Literal[
    "identification_nameplate",
    "technical_data",
    "capabilities_skills",
    "simulation_model",
    "frames_kinematics",
    "safety_workspace",
    "calibration_maintenance",
    "runtime_interfaces",
    "data_model_provenance",
    "relationships",
]
RelationshipKind = Literal[
    "isDigitalTwinOf",
    "hasDigitalTwin",
    "mountedOn",
    "composedOf",
    "observes",
    "locatedIn",
    "represents",
]
ArtifactRole = Literal[
    "unity_prefab",
    "unity_scene_package",
    "urdf",
    "xacro",
    "srdf",
    "usd",
    "visual_meshes",
    "collision_meshes",
    "materials_textures",
    "controller_assembly",
    "reset_profile",
    "frame_unit_contract",
    "camera_intrinsics",
    "camera_extrinsics",
    "workspace_geometry",
    "calibration",
    "vendor_documentation",
    "license",
    "build_attestation",
    "validation_report",
    "mcap_evidence",
    "preview_media",
]


def _ensure_unique(values: list[object], label: str) -> None:
    normalized = [str(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must be unique")


class AssetRelationshipV1(FleetModel):
    relationship: RelationshipKind
    target_global_asset_id: AasIdentifier
    target_asset_id: SafeId | None = None


class GlobalAssetReferenceV1(FleetModel):
    asset_id: SafeId
    global_asset_id: AasIdentifier
    aas_id: AasIdentifier
    identity_revision: int = Field(ge=1)
    runtime_class: RuntimeClass

    @model_validator(mode="after")
    def identifiers_are_distinct(self) -> Self:
        if self.global_asset_id == self.aas_id:
            raise ValueError("global_asset_id and aas_id have distinct meanings")
        return self


class GlobalAssetIdentityV1(GlobalAssetReferenceV1):
    schema_version: Literal[1] = 1
    asset_type: AssetType
    display_name: ShortText
    lifecycle_state: Literal["active", "decommissioned"] = "active"
    projection_mode: Literal["read_only"] = "read_only"
    submodel_slots: list[SubmodelSlot] = Field(min_length=1, max_length=10)
    relationships: list[AssetRelationshipV1] = Field(
        default_factory=list,
        max_length=256,
    )

    @model_validator(mode="after")
    def identity_relationships_are_coherent(self) -> Self:
        _ensure_unique(self.submodel_slots, "AAS submodel slots")
        relationship_keys = [
            (item.relationship, item.target_global_asset_id)
            for item in self.relationships
        ]
        _ensure_unique(relationship_keys, "asset relationships")
        if any(
            item.target_global_asset_id == self.global_asset_id
            for item in self.relationships
        ):
            raise ValueError("asset relationships cannot target the same global identity")
        if any(
            item.relationship == "isDigitalTwinOf"
            for item in self.relationships
        ) and self.runtime_class != "simulation":
            raise ValueError("isDigitalTwinOf is valid only for a simulation asset")
        return self


class ArtifactObjectRefV1(FleetModel):
    role: ArtifactRole
    object_id: ObjectId
    media_type: MediaType
    sha256: Sha256Digest
    size_bytes: int = Field(gt=0)
    file_name: ArtifactFileName | None = None

    @model_validator(mode="after")
    def object_identity_matches_digest(self) -> Self:
        if self.object_id != f"sha256:{self.sha256}":
            raise ValueError("artifact object_id must equal sha256:<sha256>")
        return self


class VersionedObjectRefV1(FleetModel):
    object_id: SafeId
    version: int = Field(ge=1)
    sha256: Sha256Digest


class AssetPackageRefV1(FleetModel):
    asset_package_id: SafeId
    version: int = Field(ge=1)
    sha256: Sha256Digest


class SceneRefV1(FleetModel):
    scene_id: SafeId
    version: int = Field(ge=1)
    sha256: Sha256Digest


class PackageRuntimeV1(FleetModel):
    kind: Literal["UNITY", "ISAAC_SIM", "ROS2_AGENT", "GENERIC"]
    version: ShortText
    ros_distribution: SafeId | None = None
    controller_contract: VersionedContractRef | None = None

    @model_validator(mode="after")
    def runtime_dependencies_are_explicit(self) -> Self:
        if self.kind == "ROS2_AGENT" and self.ros_distribution is None:
            raise ValueError("ROS2_AGENT packages require ros_distribution")
        if self.kind in {"UNITY", "ROS2_AGENT"} and self.controller_contract is None:
            raise ValueError(f"{self.kind} packages require controller_contract")
        return self


class PackageDependencyV1(FleetModel):
    kind: Literal["contract", "asset_package", "runtime_component"]
    ref: VersionedContractRef
    sha256: Sha256Digest


class AssetPackageV1(FleetModel):
    schema_version: Literal[1] = 1
    asset_package_id: SafeId
    version: int = Field(ge=1)
    compatible_asset_profile: SafeId
    runtime: PackageRuntimeV1
    artifacts: list[ArtifactObjectRefV1] = Field(min_length=1, max_length=512)
    dependencies: list[PackageDependencyV1] = Field(default_factory=list, max_length=256)
    frames_contract_ref: RegistryRefV1
    device_manifest_ref: ManifestRefV1
    build_attestation_ref: RegistryRefV1

    @model_validator(mode="after")
    def package_is_loadable_and_unambiguous(self) -> Self:
        _ensure_unique(
            [item.object_id for item in self.artifacts],
            "asset package artifact object IDs",
        )
        _ensure_unique(
            [(item.kind, item.ref) for item in self.dependencies],
            "asset package dependencies",
        )
        roles = {item.role for item in self.artifacts}
        required_roles = {
            "UNITY": {"unity_prefab", "unity_scene_package"},
            "ISAAC_SIM": {"usd"},
            "ROS2_AGENT": {"controller_assembly"},
            "GENERIC": roles,
        }[self.runtime.kind]
        if self.runtime.kind != "GENERIC" and not roles.intersection(required_roles):
            expected = ", ".join(sorted(required_roles))
            raise ValueError(
                f"{self.runtime.kind} package requires one of these artifact roles: {expected}"
            )
        return self


class WorldFrameV1(FleetModel):
    frame_id: SafeId
    length_unit: Literal["m"] = "m"
    angle_unit: Literal["rad"] = "rad"
    handedness: Literal["right", "left"]
    up_axis: Literal["+x", "-x", "+y", "-y", "+z", "-z"]
    forward_axis: Literal["+x", "-x", "+y", "-y", "+z", "-z"]
    fixed_timestep_s: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def axes_are_orthogonal(self) -> Self:
        if self.up_axis[-1] == self.forward_axis[-1]:
            raise ValueError("world up_axis and forward_axis must be orthogonal")
        return self


class ArticulationBindingV1(FleetModel):
    root_binding_id: UnityBindingId
    ordered_joints: list[SafeId] = Field(min_length=1, max_length=256)

    @field_validator("ordered_joints")
    @classmethod
    def joints_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "ordered articulation joints")
        return value


class ControllerBindingV1(FleetModel):
    component_binding_id: UnityBindingId
    contract_ref: VersionedContractRef


class SceneSignalBindingV1(FleetModel):
    semantic: Literal["health", "ready", "busy", "success", "failure", "fault"]
    component_binding_id: UnityBindingId
    member: SafeId


class SceneSensorBindingV1(FleetModel):
    sensor_id: SafeId
    kind: Literal["camera", "depth_camera", "point_cloud", "diagnostic"]
    frame_id: SafeId
    semantic_channels: list[SafeId] = Field(min_length=1, max_length=64)
    render_target_binding_id: UnityBindingId | None = None
    calibration_ref: RegistryRefV1 | None = None

    @field_validator("semantic_channels")
    @classmethod
    def channels_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "sensor semantic channels")
        return value


class SceneAssetBindingV1(FleetModel):
    identity: GlobalAssetReferenceV1
    asset_type: AssetType
    expected_asset_profile: SafeId
    package_ref: AssetPackageRefV1
    unity_binding_id: UnityBindingId
    diagnostic_hierarchy_path: str | None = Field(default=None, max_length=1024)
    articulation: ArticulationBindingV1 | None = None
    controller: ControllerBindingV1 | None = None
    frames: dict[SafeId, SafeId] = Field(min_length=1, max_length=128)
    collider_group: SafeId | None = None
    workspace_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=64)
    reset_contract_ref: VersionedObjectRefV1 | None = None
    outcome_contract_ref: VersionedObjectRefV1 | None = None
    signals: list[SceneSignalBindingV1] = Field(default_factory=list, max_length=16)
    sensors: list[SceneSensorBindingV1] = Field(default_factory=list, max_length=64)

    @field_validator("diagnostic_hierarchy_path")
    @classmethod
    def hierarchy_path_is_diagnostic_only(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("diagnostic hierarchy path must be relative and canonical")
        return value

    @model_validator(mode="after")
    def binding_contract_is_complete(self) -> Self:
        _ensure_unique(list(self.frames.values()), "bound frame IDs")
        _ensure_unique(
            [item.semantic for item in self.signals],
            "scene signal semantics",
        )
        _ensure_unique(
            [item.sensor_id for item in self.sensors],
            "scene sensor IDs",
        )
        _ensure_unique(
            [item.object_id for item in self.workspace_refs],
            "workspace references",
        )
        if self.identity.runtime_class != "simulation":
            raise ValueError("Scene bindings accept simulation assets only in v1")
        if self.asset_type in {"robot", "mobile_robot"}:
            if self.articulation is None or self.controller is None:
                raise ValueError("robot bindings require articulation and controller contracts")
            if self.reset_contract_ref is None or self.outcome_contract_ref is None:
                raise ValueError("robot bindings require reset and outcome contracts")
            if not {"base", "tool"}.issubset(self.frames):
                raise ValueError("robot bindings require base and tool frame mappings")
            required_signals = {"ready", "success", "failure", "fault"}
            available = {item.semantic for item in self.signals}
            if not required_signals.issubset(available):
                raise ValueError(
                    "robot bindings require ready, success, failure, and fault signals"
                )
        if self.asset_type == "camera":
            if "camera" not in self.frames:
                raise ValueError("camera bindings require a camera frame mapping")
            if not any(item.kind in {"camera", "depth_camera"} for item in self.sensors):
                raise ValueError("camera bindings require a render camera sensor")
        return self


class OutputIsolationV1(FleetModel):
    physical_output_connected: Literal[False] = False
    evidence_ref: RegistryRefV1


class SceneBindingManifestV1(FleetModel):
    schema_version: Literal[1] = 1
    scene_id: SafeId
    version: int = Field(ge=1)
    scene_artifact: ArtifactObjectRefV1
    unity_version: ShortText
    world: WorldFrameV1
    time_source: Literal["unity_simulation_clock"] = "unity_simulation_clock"
    required_packages: list[AssetPackageRefV1] = Field(min_length=1, max_length=256)
    environment_artifacts: list[ArtifactObjectRefV1] = Field(
        default_factory=list,
        max_length=256,
    )
    bindings: list[SceneAssetBindingV1] = Field(min_length=1, max_length=512)
    output_isolation: OutputIsolationV1
    build_attestation_ref: RegistryRefV1

    @model_validator(mode="after")
    def scene_references_are_exact(self) -> Self:
        if self.scene_artifact.role != "unity_scene_package":
            raise ValueError("scene_artifact must use the unity_scene_package role")
        package_keys = [
            (item.asset_package_id, item.version) for item in self.required_packages
        ]
        _ensure_unique(package_keys, "required scene packages")
        package_index = {
            (item.asset_package_id, item.version): item.sha256
            for item in self.required_packages
        }
        for binding in self.bindings:
            key = (binding.package_ref.asset_package_id, binding.package_ref.version)
            expected_digest = package_index.get(key)
            if expected_digest is None:
                raise ValueError(
                    f"binding {binding.unity_binding_id} references an undeclared package"
                )
            if expected_digest != binding.package_ref.sha256:
                raise ValueError(
                    f"binding {binding.unity_binding_id} package digest does not match"
                )
        _ensure_unique(
            [item.identity.asset_id for item in self.bindings],
            "scene asset IDs",
        )
        _ensure_unique(
            [item.identity.global_asset_id for item in self.bindings],
            "scene global asset IDs",
        )
        _ensure_unique(
            [item.identity.aas_id for item in self.bindings],
            "scene AAS IDs",
        )
        _ensure_unique(
            [item.unity_binding_id for item in self.bindings],
            "Unity binding IDs",
        )
        return self


class UnityFrameInventoryV1(FleetModel):
    semantic: SafeId
    frame_id: SafeId
    present: bool


class UnitySensorInventoryV1(FleetModel):
    sensor_id: SafeId
    present: bool


class UnityAssetInventoryV1(FleetModel):
    identity: GlobalAssetReferenceV1
    asset_type: AssetType
    package_ref: AssetPackageRefV1
    unity_binding_id: UnityBindingId
    hierarchy_path: str = Field(min_length=1, max_length=1024)
    articulation_root_binding_id: UnityBindingId | None = None
    ordered_joints: list[SafeId] = Field(default_factory=list, max_length=256)
    controller_component_binding_id: UnityBindingId | None = None
    frames: list[UnityFrameInventoryV1] = Field(min_length=1, max_length=128)
    sensors: list[UnitySensorInventoryV1] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def runtime_binding_is_unambiguous(self) -> Self:
        _ensure_unique([item.semantic for item in self.frames], "inventory frame semantics")
        _ensure_unique([item.frame_id for item in self.frames], "inventory frame IDs")
        _ensure_unique([item.sensor_id for item in self.sensors], "inventory sensor IDs")
        _ensure_unique(self.ordered_joints, "inventory articulation joints")
        if self.asset_type in {"robot", "mobile_robot"} and (
            self.articulation_root_binding_id is None
            or self.controller_component_binding_id is None
            or not self.ordered_joints
        ):
            raise ValueError("robot inventory requires articulation, joints, and controller")
        return self


class UnitySceneInventoryV1(FleetModel):
    schema_version: Literal[1] = 1
    scene_id: SafeId
    scene_version: int = Field(ge=1)
    unity_version: ShortText
    time_source: Literal["unity_simulation_clock"] = "unity_simulation_clock"
    physical_output_connected: Literal[False] = False
    bindings: list[UnityAssetInventoryV1] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def identities_are_unique(self) -> Self:
        _ensure_unique(
            [item.unity_binding_id for item in self.bindings],
            "inventory Unity binding IDs",
        )
        _ensure_unique(
            [item.identity.asset_id for item in self.bindings],
            "inventory asset IDs",
        )
        return self


class QualifiedActionImplementationV1(FleetModel):
    action_ref: CapabilityRef
    capability: CapabilityRef
    runtime: Literal["UNITY_CSHARP", "ROS2_PYTHON"]
    source_sha256: Sha256Digest
    installed_source_sha256: Sha256Digest
    build_sha256: Sha256Digest

    @model_validator(mode="after")
    def installed_source_matches_publication(self) -> Self:
        if self.source_sha256 != self.installed_source_sha256:
            raise ValueError("installed Action source digest must match publication")
        return self


class QualificationCheckV1(FleetModel):
    check_id: SafeId
    state: Literal["PASS", "FAIL"]
    detail: str = Field(min_length=1, max_length=2048)
    evidence_refs: list[ArtifactObjectRefV1] = Field(default_factory=list, max_length=64)


class SimulationQualificationV1(FleetModel):
    schema_version: Literal[1] = 1
    qualification_id: SafeId
    version: int = Field(ge=1)
    verdict: Literal["PASS", "FAIL"]
    run_id: SafeId
    scene_ref: SceneRefV1
    runtime_binding_ref: RegistryRefV1
    package_refs: list[AssetPackageRefV1] = Field(min_length=1, max_length=256)
    actions: list[QualifiedActionImplementationV1] = Field(min_length=1, max_length=256)
    adapter_build_sha256: Sha256Digest
    orchestrator_build_sha256: Sha256Digest
    build_attestation_refs: list[RegistryRefV1] = Field(min_length=1, max_length=64)
    validation_profile_ref: RegistryRefV1
    output_isolation_evidence_ref: RegistryRefV1
    physical_output_connected: Literal[False] = False
    time_source: Literal["unity_simulation_clock"] = "unity_simulation_clock"
    report_artifact: ArtifactObjectRefV1
    evidence_artifacts: list[ArtifactObjectRefV1] = Field(default_factory=list, max_length=256)
    checks: list[QualificationCheckV1] = Field(min_length=1, max_length=256)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    qualified_at: AwareDatetime
    qualified_by: SafeId

    @model_validator(mode="after")
    def qualification_is_complete_and_ordered(self) -> Self:
        _ensure_unique(
            [(item.asset_package_id, item.version) for item in self.package_refs],
            "qualification package references",
        )
        _ensure_unique(
            [item.action_ref for item in self.actions],
            "qualified Actions",
        )
        _ensure_unique(
            [item.object_id for item in self.build_attestation_refs],
            "qualification build attestations",
        )
        _ensure_unique(
            [item.check_id for item in self.checks],
            "qualification checks",
        )
        if not self.started_at <= self.finished_at <= self.qualified_at:
            raise ValueError(
                "qualification time must satisfy started_at <= finished_at <= qualified_at"
            )
        if self.report_artifact.role != "validation_report":
            raise ValueError("report_artifact must use the validation_report role")
        if self.verdict == "PASS" and any(
            item.state != "PASS" for item in self.checks
        ):
            raise ValueError("PASS qualification requires every check to pass")
        if self.verdict == "FAIL" and all(
            item.state == "PASS" for item in self.checks
        ):
            raise ValueError("FAIL qualification requires at least one failed check")
        return self


def contract_payload_sha256(model: FleetModel) -> str:
    """Return the canonical digest used by immutable cross-document references."""

    payload = json.dumps(
        model.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_asset_contract_bundle(
    identities: list[GlobalAssetIdentityV1],
    packages: list[AssetPackageV1],
    scene: SceneBindingManifestV1,
    qualification: SimulationQualificationV1,
) -> None:
    """Validate exact identity, package, scene, and qualification provenance."""

    _ensure_unique([item.asset_id for item in identities], "bundle asset IDs")
    _ensure_unique(
        [item.global_asset_id for item in identities],
        "bundle global asset IDs",
    )
    _ensure_unique([item.aas_id for item in identities], "bundle AAS IDs")
    identity_index = {item.asset_id: item for item in identities}
    for identity in identities:
        for relationship in identity.relationships:
            if relationship.target_asset_id is None:
                continue
            target = identity_index.get(relationship.target_asset_id)
            if target is None:
                raise ValueError(
                    f"relationship target asset is absent: {relationship.target_asset_id}"
                )
            if target.global_asset_id != relationship.target_global_asset_id:
                raise ValueError("relationship target global identity does not match")

    package_keys = [(item.asset_package_id, item.version) for item in packages]
    _ensure_unique(package_keys, "bundle Asset Package versions")
    package_index = {
        (item.asset_package_id, item.version): item for item in packages
    }
    required_package_refs = {
        (item.asset_package_id, item.version): item
        for item in scene.required_packages
    }
    for key, package_ref in required_package_refs.items():
        package = package_index.get(key)
        if package is None:
            raise ValueError(f"required Asset Package is absent: {key[0]}@{key[1]}")
        if contract_payload_sha256(package) != package_ref.sha256:
            raise ValueError(f"Asset Package digest does not match: {key[0]}@{key[1]}")

    for binding in scene.bindings:
        identity = identity_index.get(binding.identity.asset_id)
        if identity is None:
            raise ValueError(f"scene identity is absent: {binding.identity.asset_id}")
        expected_identity = GlobalAssetReferenceV1(
            asset_id=identity.asset_id,
            global_asset_id=identity.global_asset_id,
            aas_id=identity.aas_id,
            identity_revision=identity.identity_revision,
            runtime_class=identity.runtime_class,
        )
        if binding.identity != expected_identity:
            raise ValueError(
                f"scene identity revision does not match: {binding.identity.asset_id}"
            )
        package = package_index[
            (binding.package_ref.asset_package_id, binding.package_ref.version)
        ]
        if package.compatible_asset_profile != binding.expected_asset_profile:
            raise ValueError(
                f"binding profile does not match its Asset Package: {binding.unity_binding_id}"
            )

    if qualification.scene_ref.scene_id != scene.scene_id or (
        qualification.scene_ref.version != scene.version
    ):
        raise ValueError("qualification references a different Scene version")
    if qualification.scene_ref.sha256 != contract_payload_sha256(scene):
        raise ValueError("qualification Scene digest does not match")

    qualification_package_refs = {
        (item.asset_package_id, item.version): item
        for item in qualification.package_refs
    }
    if set(qualification_package_refs) != set(required_package_refs):
        raise ValueError("qualification package closure differs from the Scene")
    for key, package_ref in qualification_package_refs.items():
        if package_ref.sha256 != required_package_refs[key].sha256:
            raise ValueError("qualification Asset Package digest does not match")

    required_attestations = {
        (
            scene.build_attestation_ref.object_id,
            scene.build_attestation_ref.version,
            scene.build_attestation_ref.sha256,
        )
    }
    required_attestations.update(
        (
            package.build_attestation_ref.object_id,
            package.build_attestation_ref.version,
            package.build_attestation_ref.sha256,
        )
        for package in packages
        if (package.asset_package_id, package.version) in required_package_refs
    )
    qualified_attestations = {
        (item.object_id, item.version, item.sha256)
        for item in qualification.build_attestation_refs
    }
    if not required_attestations.issubset(qualified_attestations):
        raise ValueError("qualification is missing a required build attestation")

    isolation = scene.output_isolation.evidence_ref
    qualified_isolation = qualification.output_isolation_evidence_ref
    if isolation != qualified_isolation:
        raise ValueError("qualification output-isolation evidence does not match")
    if scene.time_source != qualification.time_source:
        raise ValueError("qualification time source does not match the Scene")
