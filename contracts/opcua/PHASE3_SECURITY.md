# Phase 3 Security and One Distributed Edge

Status: **IMPLEMENTING**. The isolated Central/Edge software gate passes, including secure shadow and
Gateway restart recovery. The attended physical Edge laptop run and distributed soak/reconnect evidence
are still required before this phase can be marked complete.

## Boundary

Phase 3 is read-only. It adds no OPC UA Method, writable SANG Variable, robot command endpoint, lease,
or actuator path. Admin HTTP remains bound to `127.0.0.1`. The only LAN listeners are:

- Central `8443/tcp`: Agent heartbeat v2 over TLS 1.2+ mutual TLS.
- Edge `4840/tcp`: OPC UA `Basic256Sha256`, `SignAndEncrypt`, application certificate validation and
  X.509 user authorization.

The Agent camera preview and authoritative local health endpoint remain Edge loopback-only. Camera
images are not transported through OPC UA or the heartbeat.

## Identity and PKI

- Agent identity URI: `urn:sang:agent:<agent_id>`.
- OPC UA Edge ApplicationUri: the same Agent identity URI.
- HTTPS and OPC UA runtime trust anchors are their separate issuing CAs; neither protocol trusts the
  shared offline ownership Root or the other protocol CA.
- Runtime configuration stores only canonical `secret://` references and certificate fingerprints.
- `sang_pki bootstrap-lab` exports role-separated runtime directories under `roles/central` and
  `roles/edge`. Neither role contains a CA private key.
- Only `roles/edge` may be copied to an Edge laptop. Never copy `root/`, `issuers/`, `state.json`, or
  `roles/central` to an Edge.
- Private key and HMAC files must be owned by the service UID and mode `0600` or stricter.

Refresh CRLs at least weekly with `sang_pki refresh --root <offline-pki-root> --issuer all`; generated
CRLs are valid for 30 days. New HTTPS connections reload the combined CA/CRL bundle, and the OPC UA
Gateway reloads trust and CRLs every five seconds. Copy the refreshed Edge role CRL files to the laptop,
then reconnect established OPC UA sessions so the new revocation state is applied.
If a reload finds missing, expired, or invalid CRL material, the Gateway exits and Docker restarts it;
it never continues serving with stale trust material.

OPC UA verifies that a protocol CRL exists, is signed by its configured issuing CA, and has not passed
`nextUpdate`. Missing, invalid, or expired CRL state fails closed. After refreshing or revoking, distribute
only the two Edge CRL files atomically:

```bash
scripts/sync_phase3_edge_crls.sh \
  runtime/phase3/pki-workcell-soarm-01/roles/edge \
  edge-user@192.168.0.20 \
  /absolute/edge/path/to/roles/edge
```

The sync script creates a mode-`0600`, unpredictable archive with `mktemp` on Edge, validates the returned
path, and atomically replaces each role CRL on the same filesystem. Its dry-run validates CRL lifetime
without contacting Edge.

## Replay Defense

Remote heartbeat schema v2 binds the mTLS certificate URI and fingerprint to the Agent registry. It
requires an in-window `sent_at`, UUID `boot_id`, and monotonic `sequence`. Current and retired boot
high-water marks are updated atomically in Central SQLite.

The local Agent-to-Gateway health document uses schema v2 HMAC-SHA256 in secure mode. The Gateway
requires the expected key ID and stores accepted boot, sequence, timestamp, and complete wire digest in
an Edge-local SQLite database. Unsigned snapshots, altered envelopes, sequence rollback, same-sequence
mutation, non-advancing boot rollover, and return to a retired boot fail closed across Gateway restarts.

Central and Edge clocks must be synchronized before testing. The default remote heartbeat age window is
12 seconds with at most 2 seconds of future skew. Record `timedatectl` or `chronyc tracking` evidence on
both machines and do not start the attended run while either host reports an unsynchronized clock.

## Bootstrap

Build the image once on Central:

```bash
cd "/home/sa08050/app developer/v2_ws"
docker build -t sang-fleet:jazzy -f docker/Dockerfile .
```

Read the service UID/GID and the `dialout`/`video` group IDs on the Edge laptop first. Use those values,
stable DNS names, and the actual test-LAN addresses:

```bash
SANG_EDGE_UID="$(id -u)" \
SANG_EDGE_GID="$(id -g)" \
SANG_EDGE_DIALOUT_GID="$(getent group dialout | cut -d: -f3)" \
SANG_EDGE_VIDEO_GID="$(getent group video | cut -d: -f3)" \
scripts/bootstrap_phase3_lab.sh \
  central.sang.local 192.168.0.10 \
  soarm-edge.sang.local 192.168.0.20 \
  workcell-soarm-01
```

This creates ignored runtime material under `runtime/phase3`, enrolls the Agent certificate and two
ConnectionProfiles in Central SQLite, and writes `central.env` and `edge.env`. Central and Edge service
UIDs are written independently. An identical interrupted bootstrap is resumable: existing keys,
certificates, SANs and role metadata are verified before missing state or exports are repaired. A
conflicting identity or incomplete key-only output is never overwritten.
This identical-bootstrap guarantee applies before credential rotation. After the original certificate is
revoked, bootstrap intentionally refuses to reuse its fixed output; use the rotation workflow rather than
rerunning initial bootstrap.

Non-default ports may be set before bootstrap with `CENTRAL_ADMIN_PORT`, `CENTRAL_INGRESS_PORT`,
`EDGE_OPCUA_PORT`, and `EDGE_PREVIEW_PORT`. `ROS_DOMAIN_ID` defaults to `42` and must be `0..232`. The
generated registry profile and runtime env use the same values.

Start Central:

```bash
docker compose \
  --env-file runtime/phase3/config/central.env \
  -p sang-phase3-central \
  -f docker/compose.phase3-central.yaml \
  up -d admin agent-ingress
```

Copy the repository, `runtime/phase3/pki-workcell-soarm-01/roles/edge`, and `edge.env` to the Edge
laptop. Update the three absolute `SANG_*` paths in `edge.env` for that laptop and confirm its generated
`SANG_UID`/`SANG_GID`. Do not copy Central secrets or the Central database.

For a network-only first test, start deterministic fake SO-101 ROS sources on Edge:

```bash
docker compose \
  --env-file /path/to/edge.env \
  -p sang-phase3-edge \
  -f docker/compose.phase3-edge.yaml \
  --profile fake up -d
```

For the physical SO-101 test, first update the Edge copy of
`config/bi_so101_hardware.yaml` with that laptop's stable serial aliases and camera paths. Then start the
read-only adapter containers together with Agent and Gateway; omit `--profile fake`:

```bash
docker compose \
  --env-file /path/to/edge.env \
  -p sang-phase3-edge \
  -f docker/compose.phase3-edge.yaml \
  --profile physical up -d
```

The `physical` profile runs only the read-only arm and camera adapters; it does not expose a command or
torque path. It uses the Edge host's `dialout` and `video` group IDs and only grants the serial and video
device classes. The domain must match `ROS_DOMAIN_ID` in `edge.env`. Phase 3 containers force discovery to `LOCALHOST`,
so same-domain publishers on Central or another robot PC cannot satisfy the Edge health contract.
The fake source, Agent and Gateway also share the Edge host IPC namespace because Fast DDS may select
shared-memory transport after discovery; without shared IPC, containers can discover topic names while
delivering no samples. This sharing remains confined to the dedicated Edge host.

```bash
docker compose \
  --env-file /path/to/edge.env \
  -p sang-phase3-edge \
  -f docker/compose.phase3-edge.yaml \
  up -d agent opcua-gateway
```

No test in this phase may start a follower command publisher or change torque state.
The Agent keeps its health/preview listener on Edge loopback for the Gateway but suppresses camera
`preview_url` advertisement to Central. Remote camera preview is intentionally unavailable in Phase 3;
otherwise a `127.0.0.1` URL could display an unrelated camera on the viewer's PC.

## Agent Certificate Rotation

Prepare a uniquely named second Agent certificate and registry credential on Central. This leaves the
old credential active:

```bash
source install/setup.bash
fleet_security_rotate \
  --db-path runtime/phase3/central/fleet.db \
  --pki-root runtime/phase3/pki-workcell-soarm-01 \
  --agent-id workcell-soarm-01 \
  --certificate-name workcell-soarm-01-https-client-02 \
  --credential-id workcell-soarm-01-https-02
```

Copy the refreshed `roles/edge` directory to the laptop, preserving mode `0600` for private files and
Edge-user ownership, then restart only the Agent. After Central observes heartbeats from the new
fingerprint, revoke the old credential using the previous certificate path printed above:

```bash
fleet_security_revoke \
  --db-path runtime/phase3/central/fleet.db \
  --credential-id workcell-soarm-01-https-01 \
  --pki-root runtime/phase3/pki-workcell-soarm-01 \
  --certificate /path/reported/as/previous_certificate \
  --reason "rotation complete"
```

Run `sync_phase3_edge_crls.sh` afterward and prove the old certificate cannot post a heartbeat. Never
revoke the old credential before the new fingerprint has been observed.

## Attended Acceptance

Open only Central `8443/tcp` inbound from the Edge test subnet and Edge `4840/tcp` inbound from Central.
Keep `8090` and `8101` loopback-only. Then verify:

1. Admin `http://127.0.0.1:8090` shows the remote Agent and its source health. Remote preview buttons are
   unavailable by design.
2. The secure shadow completes with no divergence:

```bash
docker compose \
  --env-file runtime/phase3/config/central.env \
  -p sang-phase3-central \
  -f docker/compose.phase3-central.yaml \
  --profile validation run --rm shadow
```

The shadow reads its endpoint and application/user/trust/CRL references from the enabled Central OPC UA
ConnectionProfile. Duplicate CLI values are rejected if they drift. Each run records the configured
endpoint, expected ApplicationUri, measured namespaces, configured peer address, pinned server
fingerprint, success time, or failure in SQLite; only namespaces are discovery-measured in this release.

3. Restart only the Edge Gateway. It reconnects and an older signed local snapshot remains rejected by
   the persisted replay database.
4. Stop Central shadow. Edge Agent, Gateway, adapters, and ROS observations continue unaffected.
5. Reject missing-client-certificate, untrusted-CA, unauthorized-user, revoked-certificate, stale,
   duplicate, and retired-boot attempts. Record commands and outcomes without exposing key contents.
6. Rehearse one Agent mTLS certificate rotation. `fleet_security_rotate` issues and enrolls the new
   credential before replacing each active role file atomically. An interrupted activation is fail-closed
   and repaired by retrying the same rotate command. Copy the refreshed Edge role, restart only
   the Agent, and confirm Central observes the new fingerprint. Then use `fleet_security_revoke` with the
   previous credential ID and certificate path reported by rotate, distribute refreshed CRLs, and prove
   the old credential is rejected. Both commands are idempotent for an identical retry.
7. Run the uninterrupted Phase 2-equivalent final gate and save its JSON report plus Central/Edge NIC byte
   counters:

```bash
docker compose \
  --env-file runtime/phase3/config/central.env \
  -p sang-phase3-central \
  -f docker/compose.phase3-central.yaml \
  --profile final-validation run --rm shadow-final-gate
```

The report must contain `"final_gate": true`, `"passed": true`, at least 7,200 strict seconds, and no
disconnect. Reconnect recovery remains a separate test. A simulator or fake-source pass does not replace
physical SO-101 evidence.

Phase 3 is complete only when all seven items have evidence. Until then, keep this file at
`IMPLEMENTING`.
