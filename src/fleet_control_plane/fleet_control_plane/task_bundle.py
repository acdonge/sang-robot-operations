"""Versioned Task bundle validation and idempotent Control Plane registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import ssl
from typing import Annotated, Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import Field, StringConstraints, model_validator
from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.operations_models import (
    AssetCreate,
    TaskDefinitionCreate,
    TaskRunCreate,
    WorkcellCreate,
)
from fleet_control_plane.safety_models import SafetyPolicyV1


class WorkflowStepV1(FleetModel):
    step_id: SafeId
    display_name: ShortText
    actor_roles: list[SafeId] = Field(min_length=1, max_length=64)
    unity_scenes: list[
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=256, strip_whitespace=True),
        ]
    ] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def references_are_unique(self) -> "WorkflowStepV1":
        if len(self.actor_roles) != len(set(self.actor_roles)):
            raise ValueError("workflow actor_roles must be unique")
        if len(self.unity_scenes) != len(set(self.unity_scenes)):
            raise ValueError("workflow unity_scenes must be unique")
        return self


class TaskBundleV1(FleetModel):
    schema_version: Literal[1] = 1
    bundle_id: SafeId
    source_project: Annotated[
        str,
        StringConstraints(min_length=1, max_length=1024, strip_whitespace=True),
    ]
    unity_editor_version: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64, strip_whitespace=True),
    ]
    processing_node_id: SafeId
    assets: list[AssetCreate] = Field(min_length=1, max_length=256)
    workcell: WorkcellCreate
    task_definition: TaskDefinitionCreate
    task_run: TaskRunCreate
    workflow: list[WorkflowStepV1] = Field(min_length=1, max_length=128)
    safety_policy: SafetyPolicyV1

    @model_validator(mode="after")
    def bundle_is_coherent(self) -> "TaskBundleV1":
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("bundle asset_ids must be unique")
        if any(asset.node_id != self.processing_node_id for asset in self.assets):
            raise ValueError("all bundle assets must belong to processing_node_id")
        if set(self.workcell.asset_ids) != set(asset_ids):
            raise ValueError("workcell asset_ids must exactly match bundle assets")

        definition = self.task_definition
        run = self.task_run
        if (
            run.task_definition_id != definition.task_definition_id
            or run.task_version != definition.version
        ):
            raise ValueError("task_run must reference the bundled task_definition")
        if run.workcell_id != self.workcell.workcell_id:
            raise ValueError("task_run must reference the bundled workcell")

        roles = {role.role: role for role in definition.roles}
        if {item.role for item in run.assignments} != set(roles):
            raise ValueError("READY bundled task_run must assign every task role")
        assets = {asset.asset_id: asset for asset in self.assets}
        for assignment in run.assignments:
            role = roles[assignment.role]
            asset = assets.get(assignment.asset_id)
            if asset is None:
                raise ValueError("task_run assignments must use bundled assets")
            if asset.asset_type != role.asset_type:
                raise ValueError(f"role {role.role} has the wrong asset type")
            if assignment.access_mode != role.access_mode:
                raise ValueError(f"role {role.role} has the wrong access mode")

        step_ids = [step.step_id for step in self.workflow]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow step_ids must be unique")
        for step in self.workflow:
            unknown = sorted(set(step.actor_roles) - set(roles))
            if unknown:
                raise ValueError(f"workflow references unknown roles: {unknown}")
        return self


class TaskBundleClient(Protocol):
    def get_configuration(self) -> dict[str, Any]: ...

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpTaskBundleClient:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        ca_file: Path | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Control Plane URL must be an HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("unencrypted Task registration is restricted to loopback")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_s = timeout_s
        self.context = (
            None
            if parsed.scheme == "http"
            else ssl.create_default_context(cafile=None if ca_file is None else str(ca_file))
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_s,
                context=self.context,
            ) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Control Plane {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, OSError) as exc:
            raise RuntimeError(
                f"Control Plane {method} {path} transport failed: {exc}"
            ) from exc

    def get_configuration(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/configuration")

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)


def load_task_bundle(path: str | Path) -> TaskBundleV1:
    return TaskBundleV1.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _asset_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": value["asset_id"],
        "asset_type": value["asset_type"],
        "display_name": value["display_name"],
        "runtime_kind": value.get("runtime_kind"),
        "node_id": value.get("node_id"),
        "binding": value.get("binding"),
        "capabilities": sorted(value.get("capabilities", [])),
    }


def _workcell_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "workcell_id": value["workcell_id"],
        "display_name": value["display_name"],
        "description": value.get("description"),
        "asset_ids": sorted(value.get("asset_ids", [])),
    }


def _definition_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_definition_id": value["task_definition_id"],
        "version": value["version"],
        "display_name": value["display_name"],
        "description": value.get("description"),
        "roles": sorted(
            (
                {
                    "role": role["role"],
                    "asset_type": role["asset_type"],
                    "access_mode": role["access_mode"],
                }
                for role in value.get("roles", [])
            ),
            key=lambda role: role["role"],
        ),
    }


def _run_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": value["run_id"],
        "task_definition_id": value["task_definition_id"],
        "task_version": value["task_version"],
        "workcell_id": value.get("workcell_id"),
        "display_name": value["display_name"],
        "assignments": sorted(
            (
                {
                    "role": assignment["role"],
                    "asset_id": assignment["asset_id"],
                    "access_mode": assignment["access_mode"],
                }
                for assignment in value.get("assignments", [])
            ),
            key=lambda assignment: assignment["role"],
        ),
    }


def _ensure_equal(kind: str, identity: str, actual: dict, desired: dict) -> None:
    if actual != desired:
        raise RuntimeError(f"existing {kind} {identity} differs from the Task bundle")


def register_task_bundle(
    bundle: TaskBundleV1,
    client: TaskBundleClient,
) -> dict[str, list[str]]:
    configuration = client.get_configuration()
    nodes = {
        item["node_id"]: item for item in configuration["processing_nodes"]
    }
    node = nodes.get(bundle.processing_node_id)
    if node is None:
        raise RuntimeError(
            f"unknown Processing Node: {bundle.processing_node_id}"
        )
    if node["lifecycle_state"] != "active":
        raise RuntimeError(
            f"Processing Node is decommissioned: {bundle.processing_node_id}"
        )

    created: list[str] = []
    reused: list[str] = []
    existing_assets = {item["asset_id"]: item for item in configuration["assets"]}
    for asset in bundle.assets:
        desired = _asset_view(asset.model_dump(mode="json"))
        existing = existing_assets.get(asset.asset_id)
        if existing is None:
            client.post_json("/api/v1/config/assets", asset.model_dump(mode="json"))
            created.append(f"asset:{asset.asset_id}")
        else:
            if existing["lifecycle_state"] != "active":
                raise RuntimeError(f"asset is decommissioned: {asset.asset_id}")
            _ensure_equal("asset", asset.asset_id, _asset_view(existing), desired)
            reused.append(f"asset:{asset.asset_id}")

    workcells = {item["workcell_id"]: item for item in configuration["workcells"]}
    desired_workcell = _workcell_view(bundle.workcell.model_dump(mode="json"))
    existing_workcell = workcells.get(bundle.workcell.workcell_id)
    if existing_workcell is None:
        client.post_json(
            "/api/v1/config/workcells",
            bundle.workcell.model_dump(mode="json"),
        )
        created.append(f"workcell:{bundle.workcell.workcell_id}")
    else:
        if existing_workcell["lifecycle_state"] != "active":
            raise RuntimeError(
                f"workcell is decommissioned: {bundle.workcell.workcell_id}"
            )
        _ensure_equal(
            "workcell",
            bundle.workcell.workcell_id,
            _workcell_view(existing_workcell),
            desired_workcell,
        )
        reused.append(f"workcell:{bundle.workcell.workcell_id}")

    definitions = {
        (item["task_definition_id"], item["version"]): item
        for item in configuration["task_definitions"]
    }
    definition_key = (
        bundle.task_definition.task_definition_id,
        bundle.task_definition.version,
    )
    desired_definition = _definition_view(
        bundle.task_definition.model_dump(mode="json")
    )
    existing_definition = definitions.get(definition_key)
    if existing_definition is None:
        client.post_json(
            "/api/v1/config/task-definitions",
            bundle.task_definition.model_dump(mode="json"),
        )
        created.append(
            f"task_definition:{definition_key[0]}@{definition_key[1]}"
        )
    else:
        _ensure_equal(
            "task definition",
            f"{definition_key[0]}@{definition_key[1]}",
            _definition_view(existing_definition),
            desired_definition,
        )
        reused.append(
            f"task_definition:{definition_key[0]}@{definition_key[1]}"
        )

    runs = {item["run_id"]: item for item in configuration["task_runs"]}
    desired_run = _run_view(bundle.task_run.model_dump(mode="json"))
    existing_run = runs.get(bundle.task_run.run_id)
    if existing_run is None:
        client.post_json(
            "/api/v1/config/task-runs",
            bundle.task_run.model_dump(mode="json"),
        )
        created.append(f"task_run:{bundle.task_run.run_id}")
    else:
        _ensure_equal(
            "task run",
            bundle.task_run.run_id,
            _run_view(existing_run),
            desired_run,
        )
        reused.append(f"task_run:{bundle.task_run.run_id}")

    return {"created": created, "reused": reused}


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register a versioned SANG Task bundle")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:18091")
    parser.add_argument("--bearer-token-file", type=Path)
    parser.add_argument("--ca-file", type=Path)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    token = (
        None
        if options.bearer_token_file is None
        else options.bearer_token_file.read_text(encoding="utf-8").strip()
    )
    bundle = load_task_bundle(options.bundle)
    result = register_task_bundle(
        bundle,
        HttpTaskBundleClient(
            options.control_plane_url,
            bearer_token=token,
            ca_file=options.ca_file,
        ),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
