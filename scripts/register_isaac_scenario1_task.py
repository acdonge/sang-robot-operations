"""Register the fixed Scenario 1 Task and validation gate through the local API.

Intent registration only: never starts a process, queues a Run, or sends control.
Run after geometry preflight pins the scene digest and the API loads that profile.
"""

import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

API = "http://127.0.0.1:18091/api/v1/workflow-authoring"
WORKSPACE = Path(__file__).resolve().parents[1]
TASK = "isaac-scenario1"
ADAPTER = "isaac-scenario1-v1"
GATE = "isaac-scenario1-validation"
NAME = "Isaac Scenario 1 + Unity observer"
DESCRIPTION = (
    "Five fixed virtual stages: carrier delivery, OMY transfer, RB5 path without arc, "
    "OMY return, carrier return. Recorded trajectories and virtual grasp/carrier fixtures "
    "do not qualify physical dynamics, grasp or welding. No physical outputs."
)


def request(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(API + path, data=body, method=method,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    workflow = json.loads((WORKSPACE / "contracts/tasks/isaac-scenario1-workflow.v1.json").read_text())
    adapter = json.loads((WORKSPACE / "contracts/tasks/isaac-scenario1-adapter.v1.json").read_text())
    if adapter["scene_sha256"] == "0" * 64:
        raise RuntimeError("Complete geometry preflight and pin the scene digest before registration")
    digest = hashlib.sha256(json.dumps(
        adapter, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    installed = next((item for item in request("GET", "/simulation-adapters")
                      if item["adapter_id"] == ADAPTER), None)
    if (installed is None or installed["profile_sha256"] != digest
            or installed.get("execution_runtime") != "isaac"
            or installed.get("profile_id") != "scenario1" or installed["scenario_id"] != TASK):
        raise RuntimeError("The local API must load the exact checked-in Scenario 1 adapter first")
    publications = request("GET", "/publications")
    publication = next((item for item in publications
                        if item["task_definition_id"] == TASK and item["task_version"] == 1), None)
    if publication is None:
        draft = request("POST", "/drafts", dict(
            request_id="isaac-scenario1-create-v1", task_definition_id=TASK,
            workflow_id=TASK, display_name=NAME, description=DESCRIPTION,
        ))
        draft = request("PUT", f"/drafts/{draft['draft_id']}", dict(
            request_id="isaac-scenario1-save-v1", expected_revision=draft["revision"],
            display_name=NAME, description=DESCRIPTION,
            roles=[dict(role=role["role"], asset_type="robot", access_mode=role["access"],
                        cardinality=role["cardinality"], requires=role["selector"]["requires"])
                   for role in workflow["roles"]],
            steps=workflow["steps"],
        ))
        validation = request("POST", f"/drafts/{draft['draft_id']}/validate", {})
        if not validation["valid"]:
            raise RuntimeError(validation)
        request("POST", f"/drafts/{draft['draft_id']}/publish", dict(
            request_id="isaac-scenario1-publish-v1", expected_revision=draft["revision"],
        ))
        publication = next(item for item in request("GET", "/publications")
                           if item["task_definition_id"] == TASK and item["task_version"] == 1)
    if not any(item["adapter_id"] == ADAPTER and item["profile_sha256"] == digest
               and item["runnable"] is True for item in publication["simulation_compatibility"]):
        raise RuntimeError("Existing immutable Task differs from the fixed Scenario 1 workflow")

    gate_body = dict(
        profile_id=GATE, display_name="Isaac Scenario 1 simulation acceptance",
        description=DESCRIPTION, task_definition_id=TASK, task_version=1, adapter_id=ADAPTER,
        required_successful_runs=1, max_duration_s=650,
        require_all_steps_succeeded=True, require_output_isolated=True, require_reset_acknowledged=True,
    )
    gate = next((item for item in request("GET", "/simulation-profiles")
                 if item["profile_id"] == GATE and item["adapter_profile_sha256"] == digest), None)
    if gate is not None:
        if any(gate[key] != value for key, value in gate_body.items()
               if key not in {"display_name", "description"}):
            raise RuntimeError("Existing immutable Scenario 1 gate has different acceptance criteria")
    else:
        gate = request("POST", "/simulation-profiles", dict(
            request_id=f"isaac-scenario1-gate-{digest[:32]}", **gate_body,
        ))
    print(json.dumps({"publication": publication, "validation_profile": gate}, indent=2))


if __name__ == "__main__":
    main()
