#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$WORKSPACE_ROOT/.." && pwd)"

ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-$HOME/isaacsim}"
OMY_USD="${OMY_USD:-$PROJECT_ROOT/issacsim_setting_usd/OMY_ai3m/usd/omy_f3m.usd}"
OMY_ISAAC_ENDPOINT="${OMY_ISAAC_ENDPOINT:-tcp://127.0.0.1:5559}"
OMY_ARTICULATION_PRIM="${OMY_ARTICULATION_PRIM:-/World/OMY/OMY/world}"
OMY_TCP_PRIM="${OMY_TCP_PRIM:-/World/OMY/OMY/link6/end_effector_flange_link/end_effector_link}"
OMY_ACTION_GRAPH_PRIM="${OMY_ACTION_GRAPH_PRIM:-/World/ActionGraph}"

ISAAC_PYTHON="$ISAAC_SIM_ROOT/python.sh"
ADAPTER="$WORKSPACE_ROOT/src/xr_omy_retarget/scripts/xr_omy_isaac.py"

if [[ ! -x "$ISAAC_PYTHON" ]]; then
  printf 'Isaac Sim Python launcher not found: %s\n' "$ISAAC_PYTHON" >&2
  exit 1
fi
if [[ ! -f "$OMY_USD" ]]; then
  printf 'OMY USD stage not found: %s\n' "$OMY_USD" >&2
  exit 1
fi
if ! env \
  -u CONDA_PREFIX \
  -u CONDA_DEFAULT_ENV \
  -u CONDA_PROMPT_MODIFIER \
  -u PYTHONPATH \
  -u PYTHONHOME \
  "$ISAAC_PYTHON" -c 'import zmq' >/dev/null 2>&1; then
  printf '%s\n' \
    'Isaac Sim Python is missing pyzmq. Install it with:' \
    "$ISAAC_PYTHON -m pip install --user 'pyzmq>=25,<28'" >&2
  exit 1
fi

args=(
  "$ADAPTER"
  --stage "$OMY_USD"
  --endpoint "$OMY_ISAAC_ENDPOINT"
  --articulation-prim "$OMY_ARTICULATION_PRIM"
  --tcp-prim "$OMY_TCP_PRIM"
  --action-graph-prim "$OMY_ACTION_GRAPH_PRIM"
  --arm-stiffness "${OMY_ARM_STIFFNESS:-5000.0}"
  --arm-damping "${OMY_ARM_DAMPING:-100.0}"
)

if [[ "${OMY_ISAAC_HEADLESS:-0}" == "1" ]]; then
  args+=(--headless)
fi
if [[ "${OMY_ISAAC_SELF_TEST:-0}" == "1" ]]; then
  args+=(--self-test)
fi
exec env \
  -u CONDA_PREFIX \
  -u CONDA_DEFAULT_ENV \
  -u CONDA_PROMPT_MODIFIER \
  -u PYTHONPATH \
  -u PYTHONHOME \
  "$ISAAC_PYTHON" "${args[@]}"
