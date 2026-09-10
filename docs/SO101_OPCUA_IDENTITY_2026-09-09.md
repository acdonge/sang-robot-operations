# SO101 OPC UA Identity Deployment Evidence

- Date: 2026-09-09; observations approximately 13:52-14:01 UTC.
- Stored identity consistency: **PASS**, for the old configured host only.
- Isolated security/gateway regression: **PASS**, 74 tests.
- Live deployment / secure OPC UA Read: **FAIL / NOT COMPLETED**.
- Physical observation health: **DEGRADED / UNVERIFIED**; no physical acceptance claimed.
- Changed by this workstream: this evidence file only. No runtime, certificate, script, source,
  Central database, Unity, adapter, or main-plan edits; existing dirty work was preserved.

## Planner

### Goal

Diagnose M0's obsolete SO101 endpoint/certificate identity, repair the read-only gateway only if the
repair is safely self-contained, and verify through secure OPC UA Read without Writes or Methods.

### Scope for this iteration

Read-only inspection of the deployed SO101 runtime, exact server/client/user identities, role exports,
Central ConnectionProfile, trust/CRLs, host addresses, and local Agent health. Existing deployment and
PKI tooling were inspected, not invoked to mutate live state. Parent A1/A2 implementation is separate.

### Tasks

1. Compare runtime arguments, inventory, deployment records, role metadata, certificate SANs and pins.
2. Establish whether existing tooling supports a gateway-only repair without changing shared state.
3. Attempt a bounded secure Read, run isolated regression tests, and record limitations.

### Acceptance Criteria

- The deployed listener, endpoint SAN, ApplicationUri, trust and expected identity agree and are valid.
- A live authenticated OPC UA Read succeeds; no live Write or Method is sent.
- The Agent, adapters and unrelated services are not restarted or modified.
- Missing connectivity, stale observations and incomplete acceptance remain explicitly non-passing.

### Risks / Notes

The host is no longer on the configured lab address. A new address must not be guessed from the current
NIC, and security must not be weakened to get a connection. An internally consistent old deployment
record is not evidence of a healthy live endpoint.

### Developer Instruction

Preserve the existing runtime unless endpoint, certificate, trust, deployment records and Central
expectations can be migrated together through reviewed tooling within the allowed boundary.

## Exact Identity

All paths below are relative to the workspace unless stated otherwise. The live PKI root is
`runtime/phase3-lab1/pki-processing-lab1`; its private contents were never printed.

| Field | Observed value |
| --- | --- |
| Gateway container | `sang-phase4-so101-opcua-gateway-1` |
| Agent container | `sang-phase4-so101-agent-1` |
| Agent identity | `workcell-soarm-01` |
| Robot identity | `bi_so101_01` |
| Processing Node | `processing-central-01` |
| Runtime kind / ROS domain | `physical` / `42` |
| Configured endpoint | `opc.tcp://10.0.0.8:4840/sang/` |
| Server ApplicationUri / certificate URI SAN | `urn:sang:agent:workcell-soarm-01` |
| Server IP SAN | `10.0.0.8` only; no DNS SAN |
| Server certificate subject | `CN=workcell-soarm-01-opcua-server` |
| Server certificate validity | `2026-08-21T18:05:51Z` to `2027-08-21T18:10:51Z` |
| Security | `Basic256Sha256`, `SignAndEncrypt`, X.509 user authentication |
| Central application URI | `urn:sang:central:control-plane` |
| Authorized Central user URI | `urn:sang:user:central-shadow` |
| Expected server pin reference | `secret://opcua/pins/workcell-soarm-01.pem` |
| Central profile | `workcell-soarm-01-opcua`, enabled, revision `1` |

Server certificate SHA-256, computed over DER:

```text
7a25b1a9c587df2e184f35785b3b697c9c2b396f1c5d9106dca0960003c4ce2e
```

This fingerprint agrees across the mounted Edge server certificate, both Central pin copies,
`runtime/phase4/agents/workcell-soarm-01/deployment.json`, the deployment index, and per-Agent runtime metadata.
The existing PKI `_runtime_role_result(..., edge_host="10.0.0.8",
repair_missing_metadata=False)` check passed with the workspace mounted read-only. It checks the
server URI, host SAN, certificate/private-key public-key match, exact pins, metadata, required role
material, and local-health key validity. It does **not** establish current CRL validity or reachability.

Identity sources inspected:

- `runtime/phase4/config/runtime-inventory.so101-live.yaml`
- `runtime/phase4/config/workcell-soarm-01.env` (only allowlisted non-secret fields displayed)
- `runtime/phase4/agents/workcell-soarm-01/deployment.json`
- `runtime/phase4/deployment-index.json`
- PKI `roles/central/runtime-allocations.json`
- PKI `roles/agents/workcell-soarm-01/{central,edge}/runtime-metadata.json`
- `runtime/phase3-lab1/central/fleet.db`, queried with `sqlite3 -readonly`
- Docker gateway arguments, mounts, network mode, image identity and service state

The Central profile has the same old endpoint and expected server URI. Its application, user, trust
and CRL references are respectively `secret://opcua/application/cert.pem`,
`secret://opcua/user/cert.pem`, `secret://opcua/trust/servers`, and `secret://opcua/crl`.

## Trust and Failure Diagnosis

The protocol trust anchor is `CN=SANG Lab OPCUA Issuing CA`, issued by `CN=SANG Lab Root CA`.
Central and Edge use identical OPC UA issuing-CA files, not the shared Root or HTTPS CA as runtime
trust anchors. CA validity ends `2031-08-04T20:17:58Z`; CA DER SHA-256 is
`49e73db2565a77bee48d69bed62cf03850f415feb8171123bf81e7f040db03d3`.

The Central application certificate SHA-256 is
`627e160f8cc7d1b6499893b60a8b4d1f57e9f8c8f8387bcb29f53232ca993d81`.
The Central user certificate and Edge's exact authorized-user copy both have SHA-256
`db8ce25f68ea97a94e008bcc40961378236320f366e5ce8113ddacbf667d0718`.
Both Central certificates are valid through `2027-08-05T20:17:59Z`.

Certificate chain/time checks without revocation checking passed for server, application and user.
This diagnostic check is **not** a successful secure-runtime validation: the strict check fails.
Server key, HMAC key, both Central private keys and the runtime env were all mode `0600`, UID:GID
`1000:1000`; key material was not emitted.

### 1. Expired Revocation Lists

Edge and Central OPC UA CRLs are byte-identical, with PEM-file SHA-256
`04548cdb83e641c4d37c9a2b532da060031a2a39ca8ac6e2b7603a69ed7938a9`.

- `lastUpdate`: `2026-08-05T20:16:58Z`
- `nextUpdate`: `2026-09-04T20:17:58Z`
- Signature verification against the OPC UA issuing CA: `verify OK`
- Current chain verification with `-crl_check`: exit `2`, `error 12: CRL has expired`
- Host clock: `NTPSynchronized=yes` at `2026-09-09T13:54:38Z`

The gateway was already restart-looping before this work. Its current traceback ends in
`OpcUaSecurityError: CRL has expired: /run/secrets/sang/opcua/crl/opcua-crl.pem`, then
`RuntimeError: OPC UA server failed to start`. The expired CRL prevents it reaching the listener stage.

### 2. Obsolete Bind Address

`10.0.0.8` is absent from host interfaces. The active physical NIC `enp128s31f6` has
`172.22.112.247/22`; Tailscale has `100.125.78.96/32`. Neither is covered by the server certificate.
The current NIC address is an observation, **not an approved replacement endpoint**.

- `ss -ltn 'sport = :4840'`: no listener.
- A bounded TCP connection to the configured `10.0.0.8:4840`: `TimeoutError` after 3 seconds.
- `openssl x509 ... -checkip 172.22.112.247`: explicitly reports the IP does **NOT** match.
- The gateway uses host networking, so the absent host address is not a container-local address.

Refreshing CRLs alone therefore cannot restore a valid endpoint. Changing just the Compose endpoint
would disagree with the SAN, desired profile, inventory, allocation registry and deployment records.

### 3. Agent and Source Availability

The Agent process is alive, not absent. The existing HMAC-verifying `fetch_local_health` client read
`http://127.0.0.1:8101/health/v1/robots/bi_so101_01/latest` successfully, without creating or updating
the Gateway replay database.

- Authenticated Agent/robot: `workcell-soarm-01` / `bi_so101_01`
- Snapshot time: `2026-09-09T13:57:29.838339Z`; age at validation: `1.091905` seconds
- Sequence: `648947`
- Canonical mapping digest: `e44885b3b07917889bb02e6e9e2b7c2ffb5fee6c80034dc5ff53d857fd1dd983`
- FSM: `DEGRADED`; `sources_fresh=false`; fresh source groups: `0/7`

The local Agent logs show Central heartbeat timeouts; its configured ingress is still
`https://10.0.0.8:8443`. Central's persisted `agent_status` row says `online` but was last updated
`2026-09-04T20:17:58.844Z`; it is not evidence of current connectivity. No current authenticated
Central API health verdict was obtained. Source/adapter diagnosis and Agent ingress repair are outside
this gateway workstream; no arm/camera adapter was restarted or manipulated.

## Developer

### Summary

Completed the bounded diagnosis. No safe, complete gateway-only redeployment was available in the
current scope; live state was left unchanged rather than partially repaired.

### Files Changed

- `docs/SO101_OPCUA_IDENTITY_2026-09-09.md` only.

### Commands Run

Read the requested AGENTS, requirements, README, M0 plan and M1-M5 evidence; also reviewed
`deploy/phase4/README.md`, `contracts/opcua/PHASE3_SECURITY.md`, the runtime Compose file, generator,
PKI and secure-client implementation. Inspected metadata with `jq`, public certificates/CRLs with
`openssl`, networking with `ip`/`ss`, service state/logs with Docker, and the registry with read-only
SQLite queries. No whole env, key, HMAC, token, password or private PKI state was printed.

Compose syntax validation, without starting services:

```bash
docker compose --project-name sang-phase4-so101 \
  --env-file runtime/phase4/config/workcell-soarm-01.env \
  -f docker/compose.phase4-runtime.yaml config --quiet
```

Result: exit `0`. This checks syntax, not live bind/SAN/trust health.

Regression command, executed in a disposable network-isolated container with the workspace read-only:

```bash
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,exec,nosuid,size=512m --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTHONPATH=/ws/src/sang_security:/ws/src/fleet_control_plane:/ws/src/robot_agent:/ws/src/sang_opcua_gateway \
  --volume '/home/sa08050/app developer/v2_ws:/ws:ro' --workdir /ws \
  sha256:d27c89f0cee008375797724c3380e0e688e5c4dcb62bff5b3a4b2f61dcc7a4c1 \
  python3 -m pytest -p no:cacheprovider \
  src/sang_security/test src/sang_opcua_gateway/test -q
```

### Test / Build Results

- `74 passed, 13 warnings in 17.25s`; warnings are existing `X509.get_subject` deprecations.
- The isolated tests include PKI endpoint-drift rejection, role/pin identity, CRL/security, read-only
  projection and local-health checks. Their synthetic loopback servers cannot reach physical hardware.
- `git diff --check`: exit `0` before the evidence edit. Final new-file comparison with
  `git diff --no-index --check /dev/null docs/SO101_OPCUA_IDENTITY_2026-09-09.md` produced no whitespace
  diagnostics (exit `1` for the new-file difference).
- No build, image rebuild, Fleet orchestration test suite, Unity test or physical soak was performed.
- The original gateway image is `9432106a31b257ee6bff2d5c226b4b5308e676d55e3401daac742d5699f81254`.
  Attempting an isolated test container with that image failed: `No such image`, exit `125`.
  `sang-fleet:jazzy` now resolves to the `d27c89...` image above. These tests validate current workspace
  source in the available image, not an exact reproduction of the old gateway image.

### Secure Read Attempt

At `2026-09-09T13:58:27.842919Z`, a disposable read-only client used the existing
`OpcUaClientSecurity.from_secret_refs` / `configure_secure_client`, the profile's exact pin and
trust/user references, the observed Central application URI `urn:sang:central:control-plane`, and
expected server URI `urn:sang:agent:workcell-soarm-01`.

The intended operation after secure connection was `Read` of `ServerStatus.State` (`i=2259`), using
an asyncua Client with a 3-second timeout. It failed **before connecting**:

```text
secure_opcua_read: NOT_COMPLETED
failed_stage: configure_secure_client
OpcUaSecurityError: CRL has expired:
  /ws/runtime/phase3-lab1/pki-processing-lab1/roles/central/opcua/crl/opcua-crl.pem
read_requests_sent: 0
write_requests_sent: 0
method_calls_sent: 0
```

An initial diagnostic client used an incorrect client ApplicationUri and was rejected before trust
loading; that probe was corrected from the actual certificate SAN. It is not counted as a gateway
defect. No identity, trust, CRL or encryption check was bypassed for either probe.

No remote server certificate, namespace, live ApplicationUri, application-node access attributes or
OPC UA values were measured. The last stored successful ConnectionObservation is
`2026-08-21T18:14:52.127Z`, not a fresh result; its endpoint/URI/pin must not be presented as live proof.

### Open Issues

1. The approved endpoint/network identity is unresolved. A stable, reachable address or DNS name must
   be selected by the deployment owner; binding all interfaces, using a guessed NIC address, adding
   the old lab IP, or substituting loopback would not resolve the declared deployment identity.
2. Existing generation is intentionally immutable: `scripts/generate_phase4_runtimes.sh` and
   `runtime_deploy.py` reject changed allocation/index/profile identity; PKI rejects changed
   per-Agent endpoint metadata. It also touches the Central DB, outside this bounded write scope.
3. The documented `sang_pki refresh --issuer opcua` refreshes Central, legacy Edge and all existing
   per-Agent OPC UA role CRLs, not just SO101. `--issuer all` additionally changes HTTPS trust bundles.
   Neither was run. No CA private material was copied or new ad-hoc certificate tooling introduced.
4. Recreating the gateway from the current tag would also replace its old image. A pinned deployable
   image and rollback plan must be verified instead of silently combining an image upgrade with
   identity repair.
5. The signed Agent snapshot is fresh but observations are stale, and Central ingress is obsolete.
   Even a successful gateway identity repair could not justify a physical/site health PASS.

## Evaluator

### Verdict

**FAIL** for live deployment acceptance. **PASS** for bounded diagnosis and isolated regression only.

### Summary

The exact expected SO101 identity and pins are confirmed on disk, but there is no valid live listener,
current revocation state or completed authenticated OPC UA Read. M0 remains open; this document does
not change the main plan or grant physical authorization.

### Critical Issues

- Expired OPC UA CRLs fail closed at both client and server.
- The configured/SAN host is absent and the configured endpoint times out.
- Secure session, endpoint discovery, live identity and read-only application-tree verification remain
  unperformed; no successful live Read may be claimed.

### Non-Critical Issues

- Original runtime image unavailable for fresh containers; isolated tests used the current image.
- Agent local observations are stale, and its remote heartbeat is timing out.
- Existing deprecation warnings do not affect the isolated test verdict.

### Revision Instruction

Coordinate a reviewed network/identity migration outside the present write boundary. Keep the stable
Agent, robot and ApplicationUri identities above; update endpoint SAN/certificate, exact Central pins,
inventory, allocation/index/deployment records and Central desired profile together through supported
tooling. Refresh/distribute current OPC UA CRLs with explicit shared-PKI scope. Validate routing,
permissions, expected identity and a pinned image before starting **only** `opcua-gateway` with
`--no-deps`; do not use an unqualified Compose `up`, `down` or restart of the Agent/adapters.

Then use the existing strict secure client to read the live endpoint description, certificate, namespace
array, stable Agent/robot identity nodes, and read-only access attributes. Do not invoke Methods or test
writes on the physical endpoint. Preserve truthful stale/unknown StatusCodes if the Agent sources
remain unavailable. Only that new evidence can close the gateway identity portion of M0.

## Preservation Check

All 14 non-gateway containers in the before/after running-service inventory retained identical IDs,
start timestamps and restart counts. In particular, the Agent remained started at
`2026-08-31T13:31:31.927423861Z`, the arm reader at `2026-08-31T13:31:31.932494807Z`, and the camera
reader at `2026-08-31T13:31:31.937748063Z`. The already-failing gateway continued its automatic Docker
restart loop; this work did not stop or restart it. No live control, ROS publication, service/action,
motor register write, torque change, OPC UA Write or Method was sent.
