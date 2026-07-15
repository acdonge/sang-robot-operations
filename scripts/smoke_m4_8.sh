#!/usr/bin/env bash
set -euo pipefail

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(( (RANDOM % 100) + 1 ))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
STORE_ROOT="${STORE_ROOT:-/home/sa08050/robot_store}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
TRAIN_TIMEOUT_S="${TRAIN_TIMEOUT_S:-1800}"
LOG_DIR="$(mktemp -d)"
MANIFEST_PATH="${LOG_DIR}/manifest.yaml"
gateway_pid=""

cleanup() {
    set +e
    if [[ -n "${gateway_pid}" ]] && kill -0 "${gateway_pid}" 2>/dev/null; then
        kill -TERM -- "-${gateway_pid}" 2>/dev/null || kill -TERM "${gateway_pid}"
        wait "${gateway_pid}" 2>/dev/null
    fi
    rm -rf "${LOG_DIR}"
    set -e
}

fail() {
    local message="$1"
    echo "FAIL: ${message}" >&2
    if [[ -f "${LOG_DIR}/gateway.log" ]]; then
        tail -n 160 "${LOG_DIR}/gateway.log" >&2
    fi
    if [[ -f "${LOG_DIR}/events.jsonl" ]]; then
        tail -n 80 "${LOG_DIR}/events.jsonl" >&2
    fi
    exit 1
}

wait_for_api() {
    local attempt
    for ((attempt = 0; attempt < 120; attempt++)); do
        if curl -fsS "${BASE_URL}/api/datasets" >"${LOG_DIR}/datasets.json"; then
            return 0
        fi
        if [[ -n "${gateway_pid}" ]] && ! kill -0 "${gateway_pid}" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

trap cleanup EXIT
trap 'exit 130' INT TERM

command -v lerobot-train >/dev/null || fail "lerobot-train is not installed"
python3 - <<'PY' || fail "LeRobot training extra is incomplete"
import accelerate
import wandb
PY

python3 - "${SOURCE_MANIFEST}" "${MANIFEST_PATH}" "${STORE_ROOT}" <<'PY'
from pathlib import Path
import sys
import yaml

source, output, store = map(Path, sys.argv[1:])
data = yaml.safe_load(source.read_text(encoding="utf-8"))
data["recording"]["staging_root"] = str(store / "staging")
data["recording"]["dataset_root"] = str(store / "datasets" / "raw")
output.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

cd "${WS_ROOT}"
set +u
source /opt/ros/jazzy/setup.bash
set -u
colcon build --symlink-install
set +u
source "${WS_ROOT}/install/setup.bash"
set -u

setsid ros2 run recorder_core recorder_gateway \
    "${MANIFEST_PATH}" --ui-dir "${WS_ROOT}/ui" >"${LOG_DIR}/gateway.log" 2>&1 &
gateway_pid=$!
wait_for_api || fail "gateway API did not become ready"

dataset_id="$(python3 - "${LOG_DIR}/datasets.json" <<'PY'
import json
from pathlib import Path
import sys

datasets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
eligible = [
    item for item in datasets
    if item.get("validation_status") == "PASS"
    and item.get("episodes", 0) > 0
    and item.get("frames", 0) > 0
]
if not eligible:
    raise SystemExit("no validated non-empty dataset is available")
print(eligible[0]["dataset_id"])
PY
)" || fail "no existing validated dataset was found under ${STORE_ROOT}"

export BASE_URL DATASET_ID="${dataset_id}" LOG_DIR TRAIN_TIMEOUT_S
timeout "${TRAIN_TIMEOUT_S}" python3 - <<'PY' || fail "20-step training event workflow failed"
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

import websockets


base_url = os.environ["BASE_URL"]
dataset_id = os.environ["DATASET_ID"]
log_dir = Path(os.environ["LOG_DIR"])
timeout_s = float(os.environ["TRAIN_TIMEOUT_S"])


def start_train():
    payload = json.dumps(
        {"dataset_id": dataset_id, "policy_type": "act", "steps": 20}
    ).encode("utf-8")
    request = Request(
        f"{base_url}/api/train",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


async def main():
    ws_url = base_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    ) + "/ws"
    saw_running = False
    saw_loss = False
    deadline = time.monotonic() + timeout_s
    with (log_dir / "events.jsonl").open("w", encoding="utf-8") as event_log:
        async with websockets.connect(ws_url, open_timeout=30) as websocket:
            started = await asyncio.to_thread(start_train)
            run_id = started["run_id"]
            (log_dir / "run_id").write_text(run_id, encoding="utf-8")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("training did not reach DONE")
                event = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=remaining)
                )
                if event.get("type") != "train" or event.get("run_id") != run_id:
                    continue
                event_log.write(json.dumps(event, sort_keys=True) + "\n")
                event_log.flush()
                saw_running = saw_running or event.get("state") == "RUNNING"
                saw_loss = saw_loss or (
                    event.get("state") == "RUNNING" and event.get("loss") is not None
                )
                if event.get("state") == "ERROR":
                    raise RuntimeError(f"training entered ERROR: {event}")
                if event.get("state") == "DONE":
                    if not saw_running or not saw_loss:
                        raise RuntimeError(
                            f"missing RUNNING/loss event: running={saw_running}, loss={saw_loss}"
                        )
                    return


asyncio.run(main())
PY

run_id="$(<"${LOG_DIR}/run_id")"
run_path="${STORE_ROOT}/runs/${run_id}"
[[ -f "${run_path}/lineage.json" ]] || fail "lineage.json is missing for ${run_id}"
[[ -f "${run_path}/train.log" ]] || fail "train.log is missing for ${run_id}"
if ! find "${run_path}/output/checkpoints" -type f -print -quit 2>/dev/null | grep -q .; then
    fail "no checkpoint file exists for ${run_id}"
fi

curl -fsS "${BASE_URL}/api/runs" >"${LOG_DIR}/runs.json"
python3 - "${LOG_DIR}/runs.json" "${run_id}" <<'PY' || fail "run list did not report DONE"
import json
from pathlib import Path
import sys

runs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
run = next((item for item in runs if item.get("run_id") == sys.argv[2]), None)
if run is None or run.get("state") != "DONE" or run.get("loss") is None:
    raise SystemExit(f"invalid terminal run payload: {run}")
PY

echo "PASS: M4-⑧ ${run_id} emitted RUNNING loss, reached DONE, and saved a checkpoint"
