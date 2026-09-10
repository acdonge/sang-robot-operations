# Robot/Camera Operations v1 Site Acceptance - 2026-08-26

Status: **PASS - M1 THROUGH M7 COMPLETE**

## Scope and boundary

This is the first site acceptance run for the tagged observation-only Robot/Camera Operations v1.
It does not publish robot commands, start a vendor controller, move a robot, enable an EEF or energize
welding/process I/O. Existing Central databases, objects and captures were read without deletion or
replacement.

## Initial topology

| Component | Result |
|---|---|
| Central host | `10.0.0.8`, reachable locally |
| Admin | `http://127.0.0.1:18091`, healthy after restart |
| Manager | `https://10.0.0.8:18443`, healthy after restart |
| Agent ingress | `https://10.0.0.8:8443`, valid client certificate required |
| Recording ingress | `https://10.0.0.8:8444`, valid client certificate required |
| Processing PC | `lab1` / `10.0.0.5`, online over mTLS |
| RB5 controller path | Lab router -> Lab1 `wlp0s20f3` (`10.0.2.101`) -> `10.0.2.7:5001` |
| Online canaries | `workcell-soarm-01`, `sim-isaac-omy-01` |

## Passed checks

- Git worktree was clean at `lerobot-observation-derivation-v1-20260826`.
- Central DB `PRAGMA integrity_check` returned `ok`; schema version is 11.
- Central certificate set is currently valid and expires in August 2027.
- Admin, Manager, Agent ingress and Recording ingress each recovered from a controlled restart.
- mTLS ingress rejected an anonymous client and returned healthy with the enrolled Edge certificate.
- Online Agent heartbeat sequences continued after ingress restart.
- Browser login recovered. Fleet showed Central live state and Catalog loaded 6 complete sessions,
  46.65 GiB declared data, 3 quality reports and 1 observation-only LeRobot artifact.
- Stopping only `sim-isaac-omy-01` produced an `agent_offline` event after the 30-second TTL while
  `workcell-soarm-01` continued and the immutable catalog remained at 6 sessions and 1 derivation.
- Restarting the canary created a new boot epoch and produced `agent_recovered`; replay protection
  did not reject the legitimate sequence reset.
- Isolated backup verification passed for schema versions 1 through 11, 29 objects and
  50,092,099,490 bytes. Database SHA-256 was
  `0c2afd54e06e602c28c518470a6bc9a98766c7a6d5c82097d1d5f982fb92fd9d`.
- Lab1 rejoined at DHCP address `10.0.0.5`; its authenticated heartbeat is online and Central
  records the same address as the mTLS peer. Chrony reported `locked` to Central `10.0.0.8`, with a
  2.085 ms RMS offset in the admission snapshot.
- OTOS, D455 and D405 recovered at approximately 29.8, 30.03 and 29.99 Hz. Central's robot-detail UI
  rendered each camera through the one-stream on-demand relay and released the preview lease after
  each check.
- D455 color was corrected from the depth endpoint at `video-index0` to its observed USB 2 color
  endpoint at persistent `video-index2`, UYVY 640x480 at 30 fps. Focused adapter tests passed:
  `12 passed`; the complete adapter package also passed with `17 passed`.
- The Lab1 preview certificate was rotated without weakening peer pinning. Its SANs now include
  `lab1.local`, `10.0.0.5`, `10.0.0.9` and `10.0.0.4`; direct mTLS preview returned HTTP `200` and
  Central relay playback passed.
- With all three Agents online, stopping the read-only OMY canary marked only OMY offline. Lab1's
  three camera streams and the SO-101 heartbeat continued. OMY then recovered with a new boot ID;
  Central remained at 3 Agents, 4 assets, 6 sessions, 18 artifacts, 3 quality reports and 1 dataset
  derivation.
- RB5 retained its static `10.0.2.7` address and accepted a read-only connection on port `5001`.
  Lab1's secondary `10.0.2.101/24` address was moved from disconnected `eth0` to the Wi-Fi interface
  attached to the shared router. Direct ROS measurement was 30.0 Hz; Central reported the state
  source fresh at approximately 27-29 Hz and the robot contract as `READY`, rate healthy and 4/4
  sources healthy.
- M5 is satisfied by the immutable acceptance evidence already present in this release. Task Run
  `rb5-ui-capture-acceptance-20260824` passed browser-created bounded capture, reload/restart state
  recovery, exact one-run linkage and idempotent duplicate handling. Session
  `capture-c0d93d51f619448ca378a5ce3d669e64` is `COMPLETE`, quality `PASS` and clock `LOCKED`; automatic
  upload, SHA-256 integrity and synchronized three-camera web playback passed. Derivation
  `lerobot-130a79833af4a0ccc5ad5a5b` is `COMPLETE` as one 151-frame LeRobot v3 episode, and an identical
  request returned the existing immutable result.

## M6 backup, restore and operating limits

- Backup `robot-camera-lerobot-v1-20260826T135014Z` was restored to the previously empty
  `runtime/site-acceptance-v1/restore-20260826T215236Z` path by a network-disabled container with the
  backup mounted read-only. No live Central, Agent, robot or camera endpoint was reachable from the
  restore process.
- The restore command and an independent read-only pass both reported DB SHA-256
  `0c2afd54e06e602c28c518470a6bc9a98766c7a6d5c82097d1d5f982fb92fd9d`, schema migrations 1 through
  11, 29 objects and 50,092,099,490 object bytes. The independent pass found SQLite integrity `ok`,
  zero foreign-key violations, zero missing/unexpected objects, zero size/hash mismatches and an
  exact match between restored catalog references and the manifest.
- A restore-time defect was corrected: read-only DB validation could leave
  `fleet.db.partial-wal`/`shm` files. Validation now uses SQLite immutable mode. Two focused backup
  tests passed, and rerunning validation on the physical restore left only the mode-`0600`
  `fleet.db` at the restore root.
- The restore retained 3 Agents, 4 assets, 1 Task Run, 6 recording sessions, 18 recording artifacts,
  3 quality reports, 1 LeRobot derivation, 7 point-in-time security-audit rows and 20 configuration
  audit rows. The live DB has one later successful login audit, as expected after the backup time.
- Storage policy for this host is: warning at less than 300 GiB free; stop admitting new capture or
  derivation work at less than 150 GiB free; and require projected completion to remain above the
  stop line. The completed restore leaves 254,773,911,552 bytes free, so the warning is active but
  new-work admission is not yet blocked.
- Source MCAP, quality reports, LeRobot derivations and security/configuration audit records have no
  automatic deletion. Archive/removal requires an independently hash-verified copy and explicit
  administrator approval. Audit rows are retained indefinitely in v1, with 365 days as the minimum
  site target.
- Backup and restored DB/manifest files are mode `0600` below mode-`0700` roots. No certificate,
  private-key, token or credential file exists in the backup tree. Remote anonymous Agent-registry
  access returned HTTP `401`; the unauthenticated Admin API is bound only to `127.0.0.1`. Central,
  preview-client and Processing certificates remain valid through August 2027. Login throttling is
  still delegated to the management gateway, so Manager must remain restricted to the lab
  management subnet.

## M7 two-hour observation soak

The final target was `processing-lab1 / rb5_lab1_eval_01`. Central remained at `10.0.0.8`; Lab1
remained at `10.0.0.5`; and the read-only RB5 data path remained Lab1 `wlp0s20f3` at `10.0.2.101` to
controller `10.0.2.7:5001`. No robot, vendor-controller or process command was sent.

Frozen deployment identity:

| Item | Identity / SHA-256 |
|---|---|
| Robot manifest | `manifests/rb5_lab1_eval_01.yaml` / `5fb4f55ac8e54af783f76bcc151ae8679fe65e82abbde8369dfb67f9c4dd819e` |
| Camera config | `config/rb5_lab1_cameras.yaml` / `8ac56ab9a68c378d00c7860f66a69b6e972c5c207408265b523e81f5383255ac` |
| Capture config | `config/rb5_lab1_capture.yaml` / `e0b477fa8ea4c92d2918d99f34826a195187203d6c2277af21ab0c4212bc090c` |
| Capture QoS | `config/rb5_lab1_capture_qos.yaml` / `2a2bac3e2f1541fada7eec3419835fd79612bdc998fd15f29830230665cd8592` |
| D405 | Intel `8086:0b5b`, serial `351623070728`, USB 3 at 5,000 Mbit/s |
| D455 | Intel `8086:0b5c`, serial descriptor absent, USB path `pci-0000:00:14.0-usb-0:3.1:1.0`, USB 2 color endpoint |
| OTOS WGC200 capture | Android/UVC `1f3a:100d`, serial `20080411` |
| Calibration artifact | None declared or loaded by this observation manifest; no calibration hash exists |

Strict source soak result:

- Time: `2026-08-26T22:02:08.840741Z` through `2026-08-27T00:02:08.857077Z`.
- Duration/sample count: 7,200.016 seconds / 7,144 samples.
- Result: `PASS`; unhealthy samples 0, Central fetch errors 0, stale samples 0, low-rate samples 0.
- RB5 joint state: minimum 26.059 Hz, median 28.802 Hz, maximum observed age 143.018 ms.
- OTOS: minimum 29.201 Hz, median 29.800 Hz, maximum observed age 57.260 ms.
- D455: minimum 30.019 Hz, median 30.029 Hz, maximum observed age 39.966 ms.
- D405: minimum 29.982 Hz, median 29.993 Hz, maximum observed age 47.908 ms.
- Lab1 CPU median/p95: 15.833% / 17.791%; minimum free RAM: 17,722.368 MiB.
- Report: `runtime/site-acceptance-v1/rb5-strict-soak-2h-20260826T220201Z.json`, SHA-256
  `e6b6e4e1b8657599b19a501cd2f21c4b2fdc89177c7df550cc6b5e591ae33100`.

Clock and continuity result:

- Time: `2026-08-26T22:04:15.814483Z` through `2026-08-27T00:04:15.820588Z`.
- Duration/sample count: 7,200.006 seconds / 1,439 samples.
- Result: `PASS`; offline, unhealthy and clock-unlocked samples were all 0.
- One unchanged Agent boot ID; heartbeat sequence regressions 0; source sequence regressions 0.
- Chrony reference stayed `10.0.0.8`, stratum 4. Maximum absolute system offset was 13.152341 ms,
  maximum RMS offset was 1.126166 ms, and clock steps over 20 ms were 0.
- Lab1 disk free stayed between 775.494 and 775.495 GiB.
- Report: `runtime/site-acceptance-v1/rb5-clock-continuity-2h-20260826T220201Z.json`, SHA-256
  `47080e20944cc863e873f1a213b1684fdb94dfb9213ffcd6ebee98014345767f`.

At the end, all Central and Lab1 observation containers still had zero restarts and zero OOM kills;
`processing-lab1` had no health event during the soak. The live DB again returned integrity `ok`,
zero foreign-key violations and schema 1 through 11. All 29 catalog-referenced objects totaling
50,092,099,490 bytes passed a fresh size/SHA-256 scan with zero mismatches. The Control Plane suite
passed `155` tests, the focused backup tests passed `2`, compilation passed and `git diff --check`
was clean.

## Residual risks

- Root filesystem usage is 87% with approximately 237 GiB free after retaining the temporary clean
  restore. The 300 GiB storage warning is active; the 150 GiB admission stop is not.
- The local SO-101 and OMY canary Agents are online but currently degraded because their observation
  sources are not all fresh. The fault-isolation check validates control-plane isolation, not live
  source quality.
- D455 is operating through a USB 2 color endpoint and its USB descriptor exposes no serial. A
  second identical D455 will require a persistent path-based udev identity before admission.
- Login throttling remains a management-gateway responsibility in v1. Keep Manager restricted to
  the lab management subnet.
- This Gate proves observation transport, software timestamps, catalog/storage integrity and clock
  continuity. It does not prove hardware-triggered exposure synchronization, safe robot motion,
  Unity safety decisions, welding I/O or any actuator command path.

## Final verdict

**PASS.** Robot/Camera Operations v1 is accepted for the frozen observation-only RB5/D405/D455/OTOS
topology. M1 through M7 are complete. Actuation and process-control work remain explicitly outside
this acceptance boundary.
