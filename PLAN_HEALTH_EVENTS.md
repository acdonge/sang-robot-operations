# PLAN - Health event history and SO-101 soak verification

Goal: Persist actionable Agent/source/rate transitions and provide a repeatable read-only
soak/reconnect verifier for the SO-101 workcell.

Done when:
- Robot manifests can declare minimum topic rates and the Agent reports rate health.
- Central stores deduplicated Agent, freshness, and rate transitions in SQLite.
- A read-only events API and Admin timeline expose recent transitions and recovery counts.
- A finite CLI can verify strict soak health or an expected stale/recovery cycle.
- Unit, ROS integration, browser, and a short real-hardware soak pass.

Out of scope:
- Motor commands, torque/register writes, policy execution, or Control Mode changes.
- Physical arm USB/serial unplugging or any unattended hardware manipulation.
- Remote Agent authentication/TLS, alert delivery, and long-term metrics storage.

## Milestones
- [x] M1: Add minimum-rate health to manifest and heartbeat contracts.
- [x] M2: Persist and query deduplicated health transitions in Central.
- [x] M3: Render selected-Agent event history and reconnect counts in Admin.
- [x] M4: Add a finite soak/reconnect verification CLI and tests.
- [x] M5: Run focused tests, synthetic transition integration, Playwright, and a short
  real read-only SO-101 soak; stop all verification services.
- [x] M6: Update operator documentation and complete the evaluator pass.
- [x] M7: Verify an attended physical `right_wrist` camera disconnect/reconnect and ordered
  stale/recovery events while all arm interfaces remain read-only.
- [x] M8: Run the real two-hour strict soak, retain its JSON report, inspect the Admin page,
  and stop only the validation services after results are collected.

## Decisions log
- 2026-07-21: Minimum rates are explicit per manifest source instead of inferred from robot
  type, keeping adapters for SO-101, OMY, RB5, and simulation compatible.
- 2026-07-21: Freshness and rate health remain separate fields; either can degrade a required
  source without mislabeling a low-rate stream as stale.
- 2026-07-21: Events are transition records, not per-heartbeat samples, and are capped to the
  newest 10,000 rows to avoid unbounded MVP database growth.
- 2026-07-21: The verifier only reads Central HTTP endpoints and cannot publish ROS commands.
- 2026-07-21: Agent waits three seconds before its first heartbeat by default so camera/serial
  startup does not create false disconnect history; persistent startup failures still appear.
- 2026-07-22: The attended unplug identified USB path `4.2` as the physical right-wrist camera
  and `4.4` as left wrist; the hardware config now reflects the observed physical identity.
- 2026-07-22: Camera hot-plug can assign a different `/dev/videoN`, so the validation container
  propagates host `/dev` with `rslave` and permits only video major 81 instead of bind-mounting
  the initial numbered nodes. Arm serial devices remain individually allowlisted.

## Current state
M1-M2 complete: SO-101 sources declare explicit 18 Hz arm and 25 Hz camera minimums.
Agent heartbeats keep freshness and rate health separate. Central persists transition-only
Agent/topic freshness and rate events, reconciles silent Agent stale/offline states, exposes a
filtered read-only events API, and attaches event counts to Agent snapshots. Admin now renders
low-rate state, minimum/actual Hz, recent events, and retained recovery counts. `fleet_soak`
supports strict soak and expected reconnect modes, startup readiness waiting, finite JSON
reports, and loopback-only GET transport. Forty-one focused tests pass. Synthetic ROS
integration verified a four-second `right_wrist` outage and ordered stale/recovery events.
Playwright verified actual/minimum Hz and Health history with zero console messages. A 30-second
real read-only soak collected 31 healthy samples with no stale, low-rate, or fetch errors;
minimum observed rates were about 19.99 Hz for arms and 29.48 Hz for cameras. A three-second
Agent startup grace removed false initial camera incidents. All verification services were
stopped and hardware devices released. The remaining operator-run checks are the documented
two-hour soak and an attended physical camera reconnect.

M7 completed on 2026-07-22: the attended physical right-wrist unplug/replug produced ordered
`topic_stale -> topic_recovered` events, recovered after the camera was assigned a new numbered
device, and finished healthy. The reconnect report passed with 599 samples, no fetch errors,
and no failures. M8 also passed: the strict soak ran for 7,200.003 seconds with 7,189 samples,
zero unhealthy samples, zero fetch errors, no observed issues, and a healthy final state. Every
topic had zero stale and low-rate samples; minimum arm rates were at least 19.982 Hz and minimum
camera rates were at least 29.389 Hz. The Admin page still showed `READY / 7 of 7 healthy` with
no browser console errors after the run. Reports are retained under `output/validation/`.
