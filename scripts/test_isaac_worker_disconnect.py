"""Attended local SIMULATION fault test; pauses only the dedicated pilot Worker."""

import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from register_isaac_twin_task import request

WORKER = "sang-phase3-lab1-central-isaac-workflow-worker-1"
ROOT = Path("/tmp/sang-isaac-rb5-twin")


def main():
    run = request("POST", "/simulation-runs", {
        "request_id": "isaac-disconnect-"+uuid4().hex[:12],
        "task_definition_id": "isaac-rb5-twin-pilot", "task_version": 1,
        "adapter_id": "isaac-rb5-twin-pilot-v1", "display_name": "Isaac Worker disconnect fault probe",
    })
    paused = False
    samples = []
    try:
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            heartbeat = json.loads((ROOT/"heartbeat.json").read_text())
            if heartbeat["active_command_id"] and heartbeat["current_run_id"] == run["run_id"]:
                break
            time.sleep(.02)
        else:
            raise RuntimeError("Pilot command did not become active")
        subprocess.run(["docker", "pause", WORKER], check=True, capture_output=True)
        paused = True
        started = time.monotonic()
        while time.monotonic()-started < 3.0:
            samples.append({"elapsed_s": time.monotonic()-started,
                            "heartbeat": json.loads((ROOT/"heartbeat.json").read_text()),
                            "ros": json.loads((ROOT/"observer/telemetry.json").read_text())})
            time.sleep(.025)
    finally:
        if paused:
            subprocess.run(["docker", "unpause", WORKER], check=True, capture_output=True)
    time.sleep(1)
    heartbeat = json.loads((ROOT/"heartbeat.json").read_text())
    aborted = [row for row in samples if row["heartbeat"]["lifecycle_state"] == "ABORTED"]
    if not aborted or heartbeat["lifecycle_state"] != "ABORTED":
        raise RuntimeError("Disconnect did not latch simulation Abort")
    held = [row["ros"]["joint_positions_rad"][0] for row in aborted[4:]]
    report = {"run_id": run["run_id"], "status": "PASS", "physical_output_connected": False,
              "abort_after_worker_pause_s": aborted[0]["elapsed_s"],
              "held_base_span_rad": max(held)-min(held) if held else None,
              "state_after_worker_returns": heartbeat["lifecycle_state"],
              "samples": samples, "central": request("GET", "/simulation-runs/"+run["run_id"])}
    output = Path(__file__).resolve().parents[1]/"output/validation"/f"isaac-disconnect-{run['run_id']}.json"
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ("samples", "central")}))
    print(output)


if __name__ == "__main__":
    main()
