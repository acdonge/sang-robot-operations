# Task Run UI Capture Acceptance - 2026-08-24

## Result

PASS. An Operator can request a bounded, fixed-profile MCAP recording from the Task Run UI without
SSH. The Edge pulls the durable job over mTLS, records observation topics only, uploads through the
separate recording ingress, and exposes Central integrity results in the Operator UI and Data Catalog.

## Live Setup

- Central UI/API: `http://127.0.0.1:18091`
- Agent mTLS ingress: port `8443`
- recording mTLS ingress: port `8444`
- Edge Agent: `processing-lab1`
- Capture Worker: `processing-lab1-rb5-capture`
- fixed profile: `rb5-lab1-observation-v2`
- Task Run: `rb5-ui-capture-acceptance-20260824`
- assigned assets: RB5 state, D405, D455 and OTOS WGC200

## Acceptance Evidence

- A second 20-second capture was started by clicking `녹화 시작` in a real browser, not by SSH or a
  direct API client. After a full page reload, the UI restored the same
  `capture-2693b1f247ea4ff28a4331ad1ffb2ca3` session at `UPLOADING` with `1 / 3` objects, then updated
  itself to `COMPLETE`, `VERIFIED / SHA-256`, 127.1 MiB and `3 / 3`. Its manifest SHA-256 is
  `c24b23b4662b4c6a0161d515406f4e11e6f9709a357fe683f7965d5f9d1a4220`.
- Job: `capture-86bf431a38d344c6869785ef8495f8b6`
- requested duration: 45 seconds
- state path: `PENDING -> CLAIMED -> RECORDING -> UPLOADING -> COMPLETE`
- duplicate create with request ID `ui-acceptance-20260824-1840` returned the same Job with
  `idempotent_replay=true`; no second session was created
- restarting `sang-rb5-lab1-observation-agent-1` while recording did not stop or duplicate capture
- linked session ID equals the Job ID and is attached to exactly one Task Run
- Central finalized 3/3 declared objects totaling 309,424,304 bytes
- MCAP object: 309,418,453 bytes, SHA-256
  `f53908d63902aa930f536a48d0e7813de25afcd3d450cecb644799b3d09c24e3`
- manifest SHA-256:
  `16c6c6d3995ae07e825e3bdb395534613daddbf1aed5c29bcf5ade1246fc881f`
- Catalog displayed all four topics and their message counts, all three immutable objects, object
  hashes, Task Run, Agent, robot and profile provenance
- Operator UI displayed `COMPLETE`, `VERIFIED / SHA-256`, 295.1 MiB and `3 / 3`, with a working
  deep link to the selected Catalog session

## Observation Quality Note

The capture/control pipeline passed. D405, RB5 joint state and OTOS each produced about 1,330 messages.
D455 produced only 5 messages because that existing camera source was intermittently stale during the
acceptance. This is an input-device/runtime issue to diagnose separately and did not affect Job
durability, idempotency, upload finalization or integrity verification.

## Safety Boundary

The Capture Worker accepts no executable, shell fragment, ROS topic, vendor command or writable OPC UA
input from Central. It can invoke only the checked-in recorder with its locally mounted fixed profile.
Its Compose service has no device mount, privileged mode, added capability or Docker socket. The
Operator and Admin route surfaces still contain zero robot, policy, welding or wire-feeder command
endpoints.

## Automated Verification

The focused Control Plane, Robot Agent and MCAP Recorder suites passed `277` tests. JavaScript syntax
checks and Python compilation also passed. A repository-wide collection run remains blocked only by an
unrelated stale generated XR `XROmyTarget` import under the existing build output.
