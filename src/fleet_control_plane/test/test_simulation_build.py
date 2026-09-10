from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from fleet_control_plane.control_signing import SigningKey, VerificationKeyRing
from fleet_control_plane.orchestration_api import orchestration_router
from fleet_control_plane.orchestration_store import OrchestrationStore
from fleet_control_plane.simulation_build import (
    UnityBuildProfileV1,
    attest_unity_build,
    scan_unity_build,
    scan_unity_player_artifacts,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
REPOSITORY = Path(__file__).resolve().parents[3]


def _profile() -> UnityBuildProfileV1:
    return UnityBuildProfileV1.model_validate_json(
        (REPOSITORY / "config/build_roles/unity_observer.json").read_text()
    )


def test_isolated_unity_observer_package_passes_allowlist_scan():
    report = scan_unity_build(
        REPOSITORY,
        _profile(),
        compiled_assemblies=["Sang.Observer"],
    )
    assert report.passed is True
    assert report.assembly_sha256 is not None
    assert "unity/Sang.Observer/Runtime/Sang.Observer.asmdef" in report.included_files
    assert all("SimToReal" not in path for path in report.included_files)


def test_build_scan_rejects_default_or_legacy_command_assembly(tmp_path):
    source = tmp_path / "Legacy.cs"
    source.write_text(
        "public class Bad { Rb5SimToRealUdpClient sender; }",
        encoding="utf-8",
    )
    profile = UnityBuildProfileV1(
        profile_id="unsafe-fixture",
        build_role="fixture",
        assembly_name="Unsafe.Fixture",
        include_globs=["*.cs"],
    )
    report = scan_unity_build(
        tmp_path,
        profile,
        compiled_assemblies=["Assembly-CSharp"],
    )
    assert report.passed is False
    assert {finding.code for finding in report.findings} == {
        "forbidden-assembly",
        "legacy_rb5_sender",
    }


def _artifact_report(tmp_path, profile: UnityBuildProfileV1):
    assembly = tmp_path / "Player_Data/Managed" / f"{profile.assembly_name}.dll"
    assembly.parent.mkdir(parents=True)
    assembly.write_bytes(b"safe compiled observer fixture")
    return scan_unity_player_artifacts(tmp_path, profile)


def test_player_artifact_scan_rejects_legacy_sender_and_default_assembly(tmp_path):
    profile = _profile()
    managed = tmp_path / "Player_Data/Managed"
    managed.mkdir(parents=True)
    (managed / f"{profile.assembly_name}.dll").write_bytes(
        b"metadata Rb5SimToRealUdpClient"
    )
    (managed / "Assembly-CSharp.dll").write_bytes(b"legacy")

    report = scan_unity_player_artifacts(tmp_path, profile)
    assert report.passed is False
    assert {finding.code for finding in report.findings} == {
        "forbidden-assembly",
        "legacy_rb5_sender",
    }


def test_build_attestation_requires_built_artifact_and_pinned_source_and_verifies_signature(
    tmp_path,
):
    profile = _profile()
    report = scan_unity_build(
        REPOSITORY,
        profile,
        compiled_assemblies=["Sang.Observer"],
    )
    artifact_report = _artifact_report(tmp_path, profile)
    assert artifact_report.passed is True
    signer = SigningKey.generate("unity-build-key-01")
    with pytest.raises(ValueError, match="pinned"):
        attest_unity_build(
            report,
            artifact_report=artifact_report,
            build_id="observer-build-01",
            source_repository="git://private/sang-unity",
            source_commit="abcdef1",
            unity_version="6000.5.6f1",
            scene_profile_sha256="a" * 64,
            built_at=NOW,
            signer=signer,
            source_is_pinned=False,
        )

    attestation = attest_unity_build(
        report,
        artifact_report=artifact_report,
        build_id="observer-build-01",
        source_repository="git://private/sang-unity",
        source_commit="abcdef1",
        unity_version="6000.5.6f1",
        scene_profile_sha256="a" * 64,
        built_at=NOW,
        signer=signer,
        source_is_pinned=True,
    )
    keyring = VerificationKeyRing()
    keyring.add(
        key_id=signer.key_id,
        public_key_b64=signer.public_key_b64(),
        valid_from=NOW - timedelta(minutes=1),
    )
    keyring.verify_payload(
        key_id=attestation.signer_key_id,
        signature_text=attestation.signature,
        payload=attestation,
        at=NOW,
    )
    assert json.loads(attestation.model_dump_json())["build_role"] == "observer"
    assert attestation.assemblies[0].sha256 == artifact_report.assembly_sha256

    blocked_app = FastAPI()
    blocked_app.include_router(
        orchestration_router(OrchestrationStore(tmp_path / "blocked.db"), now=lambda: NOW)
    )
    blocked = TestClient(blocked_app).post(
        "/api/v1/orchestration/unity-build-attestations",
        json=attestation.model_dump(mode="json"),
    )
    assert blocked.status_code == 503

    trusted_app = FastAPI()
    trusted_app.include_router(
        orchestration_router(
            OrchestrationStore(tmp_path / "trusted.db"),
            now=lambda: NOW,
            unity_build_keyring=keyring,
        )
    )
    registered = TestClient(trusted_app).post(
        "/api/v1/orchestration/unity-build-attestations",
        json=attestation.model_dump(mode="json"),
    )
    assert registered.status_code == 201, registered.text

    tampered = attestation.model_copy(update={"source_commit": "abcdef2"})
    rejected = TestClient(trusted_app).post(
        "/api/v1/orchestration/unity-build-attestations",
        json=tampered.model_dump(mode="json"),
    )
    assert rejected.status_code == 422


def test_source_scan_cannot_be_attested_without_a_player_artifact(tmp_path):
    profile = _profile()
    report = scan_unity_build(
        REPOSITORY,
        profile,
        compiled_assemblies=["Sang.Observer"],
    )
    missing_artifact = scan_unity_player_artifacts(tmp_path, profile)
    signer = SigningKey.generate("unity-build-key-01")

    with pytest.raises(ValueError, match="Player artifact"):
        attest_unity_build(
            report,
            artifact_report=missing_artifact,
            build_id="observer-build-01",
            source_repository="git://private/sang-unity",
            source_commit="abcdef1",
            unity_version="6000.5.6f1",
            scene_profile_sha256="a" * 64,
            built_at=NOW,
            signer=signer,
            source_is_pinned=True,
        )
