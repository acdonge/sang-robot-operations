# Phase 4 Runtime Deployment

This directory defines the Central-generated deployment input for one isolated Agent and read-only
OPC UA Gateway per robot runtime. Generation does not start Docker containers or publish robot commands.

## Inventory

Start from `runtime-inventory.so101-canary.yaml`. Its `192.0.2.x` addresses are documentation-only and
must be replaced before a live deployment.

Each runtime declares:

- stable `agent_id`, `robot_id` and `processing_node_id`;
- the Processing Node `endpoint_host`, unique OPC UA/preview ports and local `ROS_DOMAIN_ID`;
- one robot manifest, one OPC UA mapping and one directory below `agents/`;
- heartbeat interval, timeout and startup grace.

The validator rejects duplicate Agent/robot/runtime identities globally. On one Processing Node it also
rejects port and ROS-domain reuse. One endpoint host maps to one Processing Node identity, preventing two
logical hosts from accidentally sharing a listener namespace.

## Generate

The Central PKI must already be initialized. The Central data directory contains the Fleet SQLite DB.
If that DB still needs a schema migration, stop the Central API first and run this first generation in a
maintenance window. The generator refuses a missing `fleet.db` instead of silently creating a second DB
after a path typo.

```bash
cd "/home/sa08050/app developer/v2_ws"
export SANG_PKI_ROOT=/absolute/path/to/central-pki
export SANG_CENTRAL_DATA=/absolute/path/to/central-data
scripts/generate_phase4_runtimes.sh \
  deploy/phase4/runtime-inventory.so101-canary.yaml
```

Generated output is below `runtime/phase4/` by default:

- `config/<agent_id>.env`: private Compose input, mode `0600`;
- `agents/<agent_id>/deployment.json`: immutable resolved identity and contract record;
- `agents/<agent_id>/data/`: isolated Agent/Gateway state and replay DB location;
- `deployment-index.json`: Central inventory of generated runtime records.

PKI role exports remain below
`$SANG_PKI_ROOT/roles/agents/<agent_id>/{central,edge}`. Central stores the matching pin at
`roles/central/opcua/pins/<agent_id>.pem`; the ConnectionProfile references exactly that Agent-scoped pin.
The same Central role root stores `runtime-allocations.json`. It is the shared collision registry across
separate inventory files and runtime output roots; do not copy, edit or delete it on an Edge.

## Validate And Start

Validate the generated Compose input before copying an Edge role export to its Processing Node:

```bash
docker compose \
  --project-name sang-phase4-so101 \
  --env-file runtime/phase4/config/workcell-soarm-01.env \
  -f docker/compose.phase4-runtime.yaml config --quiet
```

After endpoint routing, secret transfer and an attended read-only preflight are verified, start only the
selected runtime:

```bash
docker compose \
  --project-name sang-phase4-so101 \
  --env-file runtime/phase4/config/workcell-soarm-01.env \
  -f docker/compose.phase4-runtime.yaml up -d
```

The Compose file starts observation-only Agent and Gateway processes. It contains no actuator command
publisher, writable OPC UA node or robot torque operation.

## Repeat And Change Rules

Generation is idempotent for an unchanged inventory. It fails closed instead of overwriting a changed
env, deployment record, certificate identity or existing ConnectionProfile. New runtimes may be added to
an existing index, but existing runtimes cannot be removed or mutated implicitly. Generators sharing one
PKI are serialized, and the global allocation registry prevents another output root from reserving the
same Agent, robot, Processing Node listener, ROS domain or runtime path.

`processing_node_id` is the stable machine identity used for host-local collision checks. Every runtime
on one physical PC must use the same ID even if that PC is reachable through multiple DNS names or IP
addresses; an inventory cannot infer that two different host aliases are one machine. Future live Agent
attestation will compare this declared identity with the Processing Node's reported machine identity.

An older Phase 3 OPC UA profile may use the shared legacy pin
`secret://opcua/pins/edge-cert.pem`. The M3 generator rejects that conflict before PKI or runtime writes.
Migrate or retire the legacy profile explicitly, then generate the Agent-scoped runtime. Existing
hand-maintained files in `runtime/phase4/config/` also fail closed; move their runtime to a reviewed
inventory rather than deleting or silently replacing the file. A Central ingress change likewise requires
an explicit migration instead of rewriting the deployment index. The existing heartbeat-only
`docker/compose.phase4-agent.yaml` remains the rollback path.
