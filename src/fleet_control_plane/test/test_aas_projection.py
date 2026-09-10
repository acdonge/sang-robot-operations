from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.backup import create_backup, restore_backup, verify_backup
from fleet_control_plane.aas_projection import (
    AasProjectionCatalog,
    AasProjectionInvalid,
    load_aas_model_lock,
)
from fleet_control_plane.operations_models import AssetCreate
from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane.store import FleetStore


ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "contracts/aas/metamodel.lock.yaml"
NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


def _environment(*, operation: bool = True) -> dict:
    submodel = {
        "id": "urn:sang:submodel:robot-technical-data:1",
        "idShort": "TechnicalData",
        "modelType": "Submodel",
    }
    if operation:
        submodel["submodelElements"] = [
            {
                "idShort": "DescriptiveOnlyOperation",
                "modelType": "Operation",
            }
        ]
    return {
        "assetAdministrationShells": [
            {
                "assetInformation": {
                    "assetKind": "Instance",
                    "globalAssetId": "urn:sang:global-asset:robot-01",
                },
                "id": "urn:sang:aas:robot-01",
                "idShort": "Robot01",
                "modelType": "AssetAdministrationShell",
                "submodels": [
                    {
                        "keys": [
                            {
                                "type": "Submodel",
                                "value": submodel["id"],
                            }
                        ],
                        "type": "ModelReference",
                    }
                ],
            }
        ],
        "submodels": [submodel],
    }


def _payload(environment: dict | None = None) -> bytes:
    return json.dumps(environment or _environment(), separators=(",", ":")).encode()


def _catalog(tmp_path) -> AasProjectionCatalog:
    store = FleetStore(tmp_path / "fleet.db")
    store.create_asset(
        AssetCreate(
            asset_id="robot-asset-01",
            asset_type="robot",
            display_name="Robot Asset 01",
            capabilities=["telemetry"],
        ),
        occurred_at=NOW,
    )
    return AasProjectionCatalog(
        store.path,
        FilesystemObjectStore(tmp_path / "objects"),
        LOCK,
    )


def test_idta_3_1_2_schema_is_pinned_and_valid():
    model_lock, schema = load_aas_model_lock(LOCK)

    assert model_lock.specification == "IDTA-01001-3-1-2"
    assert model_lock.release_tag == "v3.1.2"
    assert schema["$id"] == "https://admin-shell.io/aas/3/1"


def test_json_import_is_append_only_idempotent_and_output_isolated(tmp_path):
    catalog = _catalog(tmp_path)
    source = _payload()

    async def run():
        first = await catalog.import_artifact(
            source,
            asset_id="robot-asset-01",
            source_file_name="robot-01.json",
            shell_id=None,
            robotics_profile_id="robotics-reference-fixture-v1",
            opcua_endpoint="opc.tcp://127.0.0.1:4840/sang/",
            imported_by=None,
            occurred_at=NOW,
        )
        second = await catalog.import_artifact(
            source,
            asset_id="robot-asset-01",
            source_file_name="robot-01.json",
            shell_id="urn:sang:aas:robot-01",
            robotics_profile_id="robotics-reference-fixture-v1",
            opcua_endpoint="opc.tcp://127.0.0.1:4840/sang/",
            imported_by=None,
            occurred_at=NOW,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert first["projection_mode"] == "read_only"
    assert first["operation_element_count"] == 1
    assert first["submodel_ids"] == ["urn:sang:submodel:robot-technical-data:1"]
    assert catalog.list_projections(asset_id="robot-asset-01") == [first]
    assert catalog.get_projection(first["projection_id"]) == first
    assert catalog.open_artifact(first["projection_id"], environment=False).read_bytes() == source
    exported = json.loads(
        catalog.open_artifact(first["projection_id"], environment=True).read_text()
    )
    assert exported == _environment()


def test_aasx_with_one_json_environment_is_supported(tmp_path):
    catalog = _catalog(tmp_path)
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("aasx/robot-environment.json", _payload())
        archive.writestr("aasx/suppl/logo.txt", b"fixture")

    result = asyncio.run(
        catalog.import_artifact(
            stream.getvalue(),
            asset_id="robot-asset-01",
            source_file_name="robot-01.aasx",
            shell_id=None,
            robotics_profile_id=None,
            opcua_endpoint=None,
            imported_by=None,
            occurred_at=NOW,
        )
    )

    assert result["artifact_format"] == "aasx-json"
    assert result["artifact_media_type"] == (
        "application/asset-administration-shell-package"
    )


def test_schema_reference_and_link_failures_are_closed(tmp_path):
    catalog = _catalog(tmp_path)
    missing = _environment(operation=False)
    missing["submodels"] = [
        {
            "id": "urn:sang:submodel:different:1",
            "modelType": "Submodel",
        }
    ]

    with pytest.raises(AasProjectionInvalid, match="missing Submodels"):
        asyncio.run(
            catalog.import_artifact(
                _payload(missing),
                asset_id="robot-asset-01",
                source_file_name="missing.json",
                shell_id=None,
                robotics_profile_id=None,
                opcua_endpoint=None,
                imported_by=None,
                occurred_at=NOW,
            )
        )

    with pytest.raises(AasProjectionInvalid, match="supplied together"):
        asyncio.run(
            catalog.import_artifact(
                _payload(),
                asset_id="robot-asset-01",
                source_file_name="robot.json",
                shell_id=None,
                robotics_profile_id="profile-only",
                opcua_endpoint=None,
                imported_by=None,
                occurred_at=NOW,
            )
        )

    malformed = _environment()
    del malformed["assetAdministrationShells"][0]["assetInformation"]
    with pytest.raises(AasProjectionInvalid, match="schema validation failed"):
        asyncio.run(
            catalog.import_artifact(
                _payload(malformed),
                asset_id="robot-asset-01",
                source_file_name="malformed.json",
                shell_id=None,
                robotics_profile_id=None,
                opcua_endpoint=None,
                imported_by=None,
                occurred_at=NOW,
            )
        )


def test_aasx_rejects_noncanonical_members_and_xml_only_payload(tmp_path):
    catalog = _catalog(tmp_path)
    traversal = BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../environment.json", _payload())
    with pytest.raises(AasProjectionInvalid, match="non-canonical"):
        asyncio.run(
            catalog.import_artifact(
                traversal.getvalue(),
                asset_id="robot-asset-01",
                source_file_name="unsafe.aasx",
                shell_id=None,
                robotics_profile_id=None,
                opcua_endpoint=None,
                imported_by=None,
                occurred_at=NOW,
            )
        )

    xml_only = BytesIO()
    with zipfile.ZipFile(xml_only, "w") as archive:
        archive.writestr("aasx/aas-environment.xml", "<environment />")
    with pytest.raises(AasProjectionInvalid, match="only XML"):
        asyncio.run(
            catalog.import_artifact(
                xml_only.getvalue(),
                asset_id="robot-asset-01",
                source_file_name="xml-only.aasx",
                shell_id=None,
                robotics_profile_id=None,
                opcua_endpoint=None,
                imported_by=None,
                occurred_at=NOW,
            )
        )


def test_admin_api_imports_lists_and_exports_without_a_command_route(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    store.create_asset(
        AssetCreate(
            asset_id="robot-asset-01",
            asset_type="robot",
            display_name="Robot Asset 01",
            capabilities=["telemetry"],
        ),
        occurred_at=NOW,
    )
    app = create_app(
        store,
        auth_enabled=False,
        object_store=FilesystemObjectStore(tmp_path / "objects"),
        aas_model_lock_path=LOCK,
        now=lambda: NOW,
    )
    client = TestClient(app)
    source = _payload()

    created = client.post(
        "/api/v1/aas/projections",
        params={
            "asset_id": "robot-asset-01",
            "source_file_name": "robot-01.json",
        },
        content=source,
        headers={"Content-Type": "application/json"},
    )

    assert created.status_code == 201
    projection = created.json()
    projection_id = projection["projection_id"]
    assert client.get("/api/v1/aas/projections").json() == [projection]
    assert client.get(f"/api/v1/aas/projections/{projection_id}").json() == projection
    artifact = client.get(f"/api/v1/aas/projections/{projection_id}/artifact")
    assert artifact.status_code == 200
    assert artifact.content == source
    exported = client.get(
        f"/api/v1/aas/projections/{projection_id}/environment.json"
    )
    assert exported.status_code == 200
    assert exported.json() == _environment()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert not any(
        path.startswith("/api/v1/aas/")
        and any(word in path.casefold() for word in ("invoke", "execute", "command"))
        for path in paths
    )


def test_aas_api_is_unavailable_without_object_store(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    client = TestClient(create_app(store, auth_enabled=False))

    response = client.get("/api/v1/aas/projections")

    assert response.status_code == 503
    assert "Object Store" in response.json()["error"]


def test_aas_artifacts_survive_verified_backup_and_restore(tmp_path):
    catalog = _catalog(tmp_path)
    projection = asyncio.run(
        catalog.import_artifact(
            _payload(),
            asset_id="robot-asset-01",
            source_file_name="robot-01.json",
            shell_id=None,
            robotics_profile_id=None,
            opcua_endpoint=None,
            imported_by=None,
            occurred_at=NOW,
        )
    )
    backup = tmp_path / "backup"

    manifest = asyncio.run(
        create_backup(
            db_path=catalog.path,
            object_store_root=tmp_path / "objects",
            output=backup,
        )
    )

    backed_up_hashes = {item["sha256"] for item in manifest["objects"]}
    assert projection["artifact_sha256"] in backed_up_hashes
    assert projection["environment_sha256"] in backed_up_hashes
    assert verify_backup(backup)["passed"] is True
    restored_db = tmp_path / "restored/fleet.db"
    restored_objects = tmp_path / "restored/objects"
    asyncio.run(
        restore_backup(
            backup=backup,
            db_path=restored_db,
            object_store_root=restored_objects,
        )
    )
    restored = AasProjectionCatalog(
        restored_db,
        FilesystemObjectStore(restored_objects),
        LOCK,
    )
    assert restored.get_projection(projection["projection_id"]) == projection
    assert restored.open_artifact(
        projection["projection_id"], environment=False
    ).read_bytes() == _payload()
