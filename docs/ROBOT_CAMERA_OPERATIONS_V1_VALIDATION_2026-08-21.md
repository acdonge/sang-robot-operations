# Robot/Camera Operations v1 Validation

Date: 2026-08-21
Verdict: **PASS — software release gate**

## Scope

This gate validates the heterogeneous robot/camera operations backend, authenticated Admin and
Operator surfaces, selected-camera relay, selected-session MCAP catalog/Object Store, backup/restore,
and read-only Central OPC UA aggregate. It does not authorize or validate robot, EEF, welder, feeder,
or vendor-controller actuation.

## Evidence

- Main Jazzy/Isaac regression: **352 passed**, 14 dependency deprecation warnings, using the real
  `/issacsim_setting_usd` mount.
- XR package regressions: **58 passed** (`14 + 16 + 28`).
- ROS 2 clean build: **11 packages finished** in the isolated
  `output/*-operations-v1-final` build/install/log paths.
- Compose: local and remote profiles pass `docker compose config --quiet`; the remote service set is
  `central-remote`, `agent-ingress-remote`, and `opcua-aggregate-remote` with Central health ordering.
- Static checks: Python compilation, JavaScript `node --check`, and `git diff --check` pass.
- Browser: `/config.html` loaded with `config.js?v=20260821.2`; Playwright reported zero console
  errors and zero warnings. Screenshot: `output/playwright/operations-v1-config-final.png`.

## Independent Review

One successful `claude-fable-5` phase-end review was run after the initial local gate. It found one
release blocker: the new remote profile omitted the existing mTLS Agent ingress, making remote
heartbeats and peer-bound camera preview impossible without mixing in the legacy auth-disabled stack.

Disposition:

- Added `agent-ingress-remote` to `docker/compose.operations-v1.yaml` with Agent certificate/CA/CRL
  references and the shared v1 database.
- Added Central healthchecks and service ordering so Central completes schema/auth initialization
  before ingress or OPC UA starts.
- Documented exact Admin, Agent ingress, OPC UA, and Edge preview firewall boundaries and prohibited
  mixing the legacy Phase 3 Compose stack with the v1 database.
- Also closed the non-blocking redirect SSRF and early-disconnect preview-slot findings, with tests.
- Re-ran the complete 352-test suite and 11-package build after these fixes. Per phase policy, no
  second Fable invocation was made.

## Deferred Hardening

- Site-specific login throttling/security-audit retention policy.
- Site-specific Object Store capacity quota and alert thresholds.
- Concurrent `shared_read` reservation semantics across multiple Task Runs.

These do not bypass authentication, integrity, or the no-actuation boundary. They are recorded as
explicit follow-ups rather than silently implied capabilities.

## Remaining Site Acceptance

Before a physical deployment is accepted, provision real certificates and Agent registrations, run
clock synchronization checks, verify each hardware manifest/calibration, exercise selected-camera and
selected-session upload over the actual network, and complete the required attended soak. No physical
robot or welding motion was performed by this software gate.
