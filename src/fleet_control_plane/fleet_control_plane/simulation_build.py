"""Allowlist-based Unity build scanning and attestations for accepted simulator roles."""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Literal

from pydantic import Field

from fleet_control_plane.control_signing import SigningKey
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    UnityAssemblyAttestationV1,
    UnityBuildAttestationV1,
)
from fleet_control_plane.orchestration_store import canonical_json


DEFAULT_FORBIDDEN_PATTERNS = {
    "legacy_rb5_sender": r"\bRb5SimToRealUdpClient\b",
    "legacy_omy_sender": r"\bOmySimToRealUdpSender\b",
    "socket_send": r"\.(?:Send|SendTo)\s*\(",
    "ros_command_topic": r"(?:joint_trajectory|cmd_vel|digital_outputs?|analog_outputs?)",
    "vendor_command_sdk": r"(?:rbpodo|RobotCommand|MoveJ|MoveL)\s*[.(]",
}


class UnityBuildProfileV1(FleetModel):
    schema_version: Literal[1] = 1
    profile_id: SafeId
    build_role: Literal["observer", "simulation_executor", "fixture"]
    assembly_name: str = Field(min_length=1, max_length=256)
    include_globs: list[str] = Field(min_length=1, max_length=128)
    exclude_globs: list[str] = Field(default_factory=list, max_length=128)
    forbidden_assemblies: list[str] = Field(
        default_factory=lambda: ["Assembly-CSharp"], max_length=64
    )


class BuildScanFindingV1(FleetModel):
    code: SafeId
    path: str
    detail: str


class BuildScanReportV1(FleetModel):
    schema_version: Literal[1] = 1
    profile_id: SafeId
    build_role: Literal["observer", "simulation_executor", "fixture"]
    assembly_name: str = Field(min_length=1, max_length=256)
    passed: bool
    included_files: list[str]
    assembly_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    findings: list[BuildScanFindingV1]


class UnityArtifactScanReportV1(FleetModel):
    schema_version: Literal[1] = 1
    profile_id: SafeId
    build_role: Literal["observer", "simulation_executor", "fixture"]
    assembly_name: str = Field(min_length=1, max_length=256)
    passed: bool
    artifact_paths: list[str]
    assembly_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    findings: list[BuildScanFindingV1]


def scan_unity_build(
    source_root: str | Path,
    profile: UnityBuildProfileV1,
    *,
    compiled_assemblies: list[str],
) -> BuildScanReportV1:
    root = Path(source_root).resolve()
    included: set[Path] = set()
    for pattern in profile.include_globs:
        included.update(path.resolve() for path in root.glob(pattern) if path.is_file())
    for pattern in profile.exclude_globs:
        included.difference_update(
            path.resolve() for path in root.glob(pattern) if path.is_file()
        )

    findings: list[BuildScanFindingV1] = []
    if not included:
        findings.append(
            BuildScanFindingV1(
                code="empty-source-set",
                path=".",
                detail="build profile did not resolve any source files",
            )
        )
    for assembly in sorted(set(compiled_assemblies) & set(profile.forbidden_assemblies)):
        findings.append(
            BuildScanFindingV1(
                code="forbidden-assembly",
                path=assembly,
                detail="accepted build contains an assembly outside the role allowlist",
            )
        )

    digest = hashlib.sha256()
    included_names: list[str] = []
    for path in sorted(included):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            findings.append(
                BuildScanFindingV1(
                    code="source-outside-root",
                    path=str(path),
                    detail="resolved source escaped the configured source root",
                )
            )
            continue
        content = path.read_bytes()
        included_names.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        if path.suffix.lower() not in {".cs", ".asmdef"}:
            continue
        text = content.decode("utf-8", errors="replace")
        for code, pattern in DEFAULT_FORBIDDEN_PATTERNS.items():
            if re.search(pattern, text, flags=re.IGNORECASE):
                findings.append(
                    BuildScanFindingV1(
                        code=code,
                        path=relative,
                        detail=f"source matched prohibited pattern {pattern}",
                    )
                )

    return BuildScanReportV1(
        profile_id=profile.profile_id,
        build_role=profile.build_role,
        assembly_name=profile.assembly_name,
        passed=not findings,
        included_files=included_names,
        assembly_sha256=digest.hexdigest() if included_names else None,
        findings=findings,
    )


def scan_unity_player_artifacts(
    build_root: str | Path,
    profile: UnityBuildProfileV1,
) -> UnityArtifactScanReportV1:
    """Verify the actual role assembly; a source scan alone cannot be attested."""

    root = Path(build_root).resolve()
    dlls = sorted(path.resolve() for path in root.rglob("*.dll") if path.is_file())
    expected_name = f"{profile.assembly_name}.dll"
    targets = [path for path in dlls if path.name == expected_name]
    findings: list[BuildScanFindingV1] = []
    for path in dlls:
        if path.stem in profile.forbidden_assemblies:
            findings.append(
                BuildScanFindingV1(
                    code="forbidden-assembly",
                    path=path.relative_to(root).as_posix(),
                    detail="built Player contains an assembly outside the role allowlist",
                )
            )
    if len(targets) != 1:
        findings.append(
            BuildScanFindingV1(
                code="role-assembly-count-invalid",
                path=expected_name,
                detail=f"expected exactly one built role assembly, found {len(targets)}",
            )
        )

    artifact_paths = [path.relative_to(root).as_posix() for path in dlls]
    assembly_sha256 = None
    if len(targets) == 1:
        content = targets[0].read_bytes()
        assembly_sha256 = hashlib.sha256(content).hexdigest()
        binary_text = content.decode("latin-1", errors="ignore")
        for code, pattern in DEFAULT_FORBIDDEN_PATTERNS.items():
            if re.search(pattern, binary_text, flags=re.IGNORECASE):
                findings.append(
                    BuildScanFindingV1(
                        code=code,
                        path=targets[0].relative_to(root).as_posix(),
                        detail=f"built role assembly matched prohibited pattern {pattern}",
                    )
                )
    return UnityArtifactScanReportV1(
        profile_id=profile.profile_id,
        build_role=profile.build_role,
        assembly_name=profile.assembly_name,
        passed=not findings,
        artifact_paths=artifact_paths,
        assembly_sha256=assembly_sha256,
        findings=findings,
    )


def attest_unity_build(
    report: BuildScanReportV1,
    *,
    artifact_report: UnityArtifactScanReportV1,
    build_id: str,
    source_repository: str,
    source_commit: str,
    unity_version: str,
    scene_profile_sha256: str,
    built_at: datetime,
    signer: SigningKey,
    source_is_pinned: bool,
) -> UnityBuildAttestationV1:
    if not report.passed or report.assembly_sha256 is None:
        raise ValueError("only a passing build scan can be attested")
    if (
        not artifact_report.passed
        or artifact_report.assembly_sha256 is None
        or artifact_report.profile_id != report.profile_id
        or artifact_report.build_role != report.build_role
        or artifact_report.assembly_name != report.assembly_name
    ):
        raise ValueError("attestation requires a matching passing Player artifact scan")
    if not source_is_pinned:
        raise ValueError("production attestation requires a pinned source repository")
    unsigned = UnityBuildAttestationV1(
        build_id=build_id,
        build_role=report.build_role,
        source_repository=source_repository,
        source_commit=source_commit,
        unity_version=unity_version,
        scene_profile_sha256=scene_profile_sha256,
        assemblies=[
            UnityAssemblyAttestationV1(
                assembly_name=report.assembly_name,
                sha256=artifact_report.assembly_sha256,
            )
        ],
        built_at=built_at,
        signer_key_id=signer.key_id,
        signature="unsigned-build-0",
    )
    return unsigned.model_copy(update={"signature": signer.sign_payload(unsigned)})


def attestation_payload_sha256(attestation: UnityBuildAttestationV1) -> str:
    return hashlib.sha256(
        canonical_json(
            attestation.model_dump(mode="json", exclude={"signature"})
        ).encode("utf-8")
    ).hexdigest()
