# RB5 Four-Source Automatic Upload One-Hour Result - 2026-08-24

## Verdict

**PASS.** Lab1 recorded RB5 joint state, D455, D405, and OTOS continuously for one hour, preserved a complete MCAP session, and transferred it to the Central immutable Object Store over the dedicated Agent-authenticated mTLS ingress.

## Session

- Agent: `processing-lab1`
- Robot: `rb5_lab1_eval_01`
- Session: `rb5-lab1-observation-v2-20260824T160243Z-135ccda2`
- Recorded interval: `2026-08-24T16:02:44.399110537Z` to `2026-08-24T17:02:43.915522386Z`
- Recorded duration: 3,599.516 seconds
- Total declared size: 32,478,996,343 bytes
- MCAP size: 32,478,990,484 bytes
- Recorder exit: 0
- Reported transport messages lost: 0
- Safety mode: read-only observation; robot and vendor command channels disabled

## Source Evidence

| Source | Messages | Timestamp regressions | Median observed rate | Minimum observed rate | Stale / low-rate samples |
| --- | ---: | ---: | ---: | ---: | ---: |
| RB5 joints | 107,986 | 0 | 30.001 Hz | 29.396 Hz | 0 / 0 |
| D455 color | 107,855 | 0 | 29.964 Hz | 29.958 Hz | 0 / 0 |
| D405 color | 107,960 | 0 | 29.993 Hz | 29.986 Hz | 0 / 0 |
| OTOS welding view | 107,058 | 0 | 29.800 Hz | 29.200 Hz | 0 / 0 |

The Central health monitor ran for 4,800.014 seconds so it covered both the one-hour capture and the post-capture upload. It collected 4,767 one-second samples with zero API fetch errors, zero unhealthy samples, and no observed issues. Processing PC CPU was 8.599% median and 9.840% p95; minimum free RAM was 19,473.408 MiB.

## Upload And Integrity

- Central catalog state: `COMPLETE`
- Completed artifacts: 3/3
- Upload attempts: 1
- Upload error: none
- Canonical manifest SHA-256 on Edge and Central: `3e2a658b831a09b2d762b4212686223084933998268021634037703bef89fba2`
- `metadata.yaml` SHA-256: `d2a8b2f5404a36255ae29883e995b23737884aef21d4122f1be482c9fef94944`
- MCAP SHA-256: `763316efc5a0e7a9e1db1b5576ea4909a6a8b7173728dd8f85603e5bca08fd2a`
- `recorder.log` SHA-256: `02696734f1001e2dcff887f2baad39aa8b3eabd37616cd35b642d81596b64205`

Independent `sha256sum` checks matched for every declared artifact on Edge and Central. The raw manifest JSON byte hash differs because Central intentionally stores its canonical sorted and compact representation; parsing either representation produces the same canonical digest above.

All RB5, camera, Agent, uploader, Central Admin, and recording-ingress containers remained free of restarts and OOM termination. Remaining free storage after replication was approximately 789 GB on Lab1 and 399 GB on Central.

## Boundary Of This Result

This result validates sustained capture, source freshness and rates, timestamp monotonicity, automatic upload, heartbeat continuity, and storage integrity. It does **not** prove hard real-time synchronization. The capture manifest still marks clock quality as `UNVERIFIED`: RB5 timestamps are assigned after the vendor data read, and camera timestamps are assigned when host capture returns rather than at hardware exposure. No PTP/NTP offset evidence or nearest camera-to-joint skew measurement was collected in this acceptance.
