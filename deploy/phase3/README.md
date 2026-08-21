# Phase 3 Deployment Files

- `central.env.example`: Central host paths and the remote Edge endpoint.
- `edge.env.example`: Edge laptop paths, Central ingress URL, ROS domain, and local endpoint.
- `docker/compose.phase3-central.yaml`: loopback Admin, LAN mTLS Agent ingress, short secure shadow, and
  separate uninterrupted final-gate shadow.
- `docker/compose.phase3-edge.yaml`: read-only Agent, secure OPC UA Gateway, optional fake SO-101 sources.
- `scripts/bootstrap_phase3_lab.sh`: verified/resumable pre-rotation PKI bootstrap, role export,
  idempotent initial registry enrollment, independent Central/Edge UID configuration, and environment
  generation.
- `scripts/prepare_phase3_edge_runtime.sh`: creates the Edge data and capture directories with the
  configured UID/GID before Compose starts. Run it on each Edge after copying `edge.env`; Agent
  capture mounts deliberately refuse Docker's root-owned auto-creation fallback.
- `scripts/sync_phase3_edge_crls.sh`: seven-day CRL validity check and atomic SSH distribution to Edge.

Follow `contracts/opcua/PHASE3_SECURITY.md`. Generated keys, runtime databases, reports, and edited env
files belong under ignored `runtime/`; do not commit them.

```bash
./scripts/prepare_phase3_edge_runtime.sh /absolute/path/to/edge.env
```
