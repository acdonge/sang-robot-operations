# Recording Inventory Validation - 2026-08-06

## Result

Phase 3.6 implements a read-only, metadata-only local MCAP inventory from
`processing-lab1` to the Phase 3 Central Admin. The live endpoint is
`http://127.0.0.1:18091/` and the robot detail route is:

```text
http://127.0.0.1:18091/#robot/processing-lab1/rb5_lab1_eval_01
```

## Live Evidence

- Agent: `processing-lab1`, heartbeat schema v2 over mTLS
- Inventory state: `ready`
- Complete: 1
- Failed: 1
- Partial: 0
- Invalid: 0
- Declared complete size: 73,216,896 bytes (69.8 MiB)
- Accepted session: 3 topics, 678 messages, transport loss 0
- Failed session: transport loss 14
- Recent ordering: manifest `finished_at`, newest first
- Agent API response size with two sessions: 3,106 bytes
- Forbidden raw fields checked absent: file paths, MCAP filenames, checksums,
  recorder log, and raw image/video bytes

The RB5 network helper, RB5 read-only state adapter, OTOS camera, and D455
camera retained the same container IDs, start timestamps, and restart count 0.
Only the Lab1 Robot Agent was recreated after Central Admin and mTLS ingress
were upgraded first.

## Verification

- Focused scanner tests: 7 passed
- Focused heartbeat/model/API/UI tests: passed
- Focused post-review contract tests: 44 passed
- Remote Lab1 scanner and heartbeat tests: 17 passed
- ROS 2 build: 11 packages passed
- Core Fleet/Agent/OPC UA/MCAP/adapter/simulation tests: 261 passed, 14 warnings
- XR tests, run per package to avoid duplicate pytest module names: 58 passed
- `git diff --check`, Compose config, Python syntax, and JavaScript syntax: passed
- Desktop DOM and visual inspection: passed
- Mobile 390 x 844 visual inspection: passed

The first aggregate test invocation omitted the documented read-only
`/issacsim_setting_usd` mount. It produced one expected asset-hash failure;
rerunning the simulator suite with the vendor asset mount passed 11/11.

## Safety And Data Boundary

- No robot, vendor command-channel, recorder-control, upload, or delete API was
  added.
- The scanner opens session directories and manifests with `O_NOFOLLOW` and
  never reads MCAP payloads.
- Root discovery is capped at 10,000 entries, manifest candidates at 1,000,
  recent heartbeat metadata at 20 sessions, and each manifest at 128 KiB.
- Legacy heartbeat payloads receive an additive disabled inventory default.
- Operator Task Run responses omit the Agent-wide inventory so metadata from
  unrelated runs is not exposed in the scoped user API.
- `.partial` is deliberately reported as `PARTIAL`, not `ACTIVE`, because a
  directory alone cannot prove that a recorder process is alive.

## Remaining Operational Notes

- The Phase 3 Lab1 Central uses port 18091 and its own secure fleet database.
  The existing multi-robot local console on port 8090 is a separate runtime;
  merging them belongs to the multi-edge Central consolidation step.
- OTOS `welding_view` still oscillates around the 25 Hz health threshold and
  creates pre-existing low-rate/recovery event noise. Recording inventory does
  not change or hide that data-quality debt.
- Central Object Storage upload, durable artifact catalog, retention, LeRobot
  derivation, and remote recording lifecycle controls remain deferred.

## Phase-End Review

The one-time `claude-fable-5` review initially returned `FAIL`. It identified
three Central-contract gaps: entity identifiers could exceed 64 characters,
`finished_at` could precede `started_at`, and final/partial directory collisions
could emit duplicate session IDs. The revision aligns identifier bounds,
rejects reversed timestamps, invalidates collisions, deduplicates recent IDs,
adds a real scanner-to-Pydantic round-trip test, and treats degraded inventory
as needing attention.

A focused read-only Opus follow-up returned `PASS` with no critical findings.
It also confirmed that `/var/lib/sang` remains writable for ROS logs while the
nested capture mount is read-only. A deployment-only regression found during
that change was recovered without recreating RB5, OTOS, or D455 observation
containers. New Edge provisioning now pre-creates `captures` with the configured
UID/GID and Compose refuses an absent bind source rather than auto-creating a
root-owned directory.

Remaining non-critical debt: inventory scanning still runs synchronously on
the Agent heartbeat path; move it to a background cache before substantially
larger capture catalogs. A final/partial name collision is intentionally
reported as invalid even if one directory contains a legitimate completed
session, favoring an unambiguous heartbeat over guessing which copy is valid.
