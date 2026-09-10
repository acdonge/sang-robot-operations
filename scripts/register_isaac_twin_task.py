"""Register the checked-in pilot Task through the normal local authoring API.

This registers intent only. It neither starts a process nor queues a motion Run.
"""

import json
from pathlib import Path
from urllib.request import Request, urlopen

API = "http://127.0.0.1:18091/api/v1/workflow-authoring"
TASK = "isaac-rb5-twin-pilot"
NAME = "Isaac RB5 + Unity digital twin"


def request(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(API + path, data=body, method=method,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    publications = request("GET", "/publications")
    if any(p["task_definition_id"] == TASK for p in publications):
        print("Pilot Task already published; not modifying immutable version")
        return
    workflow = json.loads((Path(__file__).resolve().parents[1] /
                           "contracts/tasks/isaac-rb5-twin-workflow.v1.json").read_text())
    draft = request("POST", "/drafts", dict(
        request_id="isaac-twin-pilot-create-v1", task_definition_id=TASK,
        workflow_id=TASK, display_name=NAME,
        description="Isolated virtual RB5. Isaac executes two bounded joint actions; Unity observes ROS feedback. No physical outputs.",
    ))
    draft = request("PUT", f"/drafts/{draft['draft_id']}", dict(
        request_id="isaac-twin-pilot-save-v1", expected_revision=draft["revision"],
        display_name=NAME, description=draft["description"],
        roles=[dict(role="virtual-robot", asset_type="robot", access_mode="exclusive",
                    cardinality="1", requires=["simulate-bounded-joint@1"])],
        steps=workflow["steps"],
    ))
    validation = request("POST", f"/drafts/{draft['draft_id']}/validate", {})
    if not validation["valid"]:
        raise RuntimeError(validation)
    publication = request("POST", f"/drafts/{draft['draft_id']}/publish", dict(
        request_id="isaac-twin-pilot-publish-v1", expected_revision=draft["revision"],
    ))
    print(json.dumps(publication, indent=2))


if __name__ == "__main__":
    main()
