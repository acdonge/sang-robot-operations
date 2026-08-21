# RB5 Lab1 Local MCAP Validation - 2026-08-06

## Scope

Validate the Phase 3.5 local MCAP worker on `processing-lab1` with live,
read-only RB5 measured joints, OTOS welding camera JPEG frames, and D455 color
JPEG frames. No robot command, welding arc, image decode, or image re-encode
was used.

## First Run And Correction

The first 10-second run produced an MCAP file, but ROS bag reported 14
transport-layer message losses. The initial implementation did not preserve or
gate on this warning and incorrectly finalized the session.

The implementation was corrected before acceptance:

- retain ROS bag output as `recorder.log` and include it in SHA-256 checksums;
- parse the ROS bag transport-loss diagnostic and reject any count above zero;
- apply explicit `BEST_EFFORT`, `KEEP_LAST`, depth 100 subscriber QoS to all
  three allowlisted topics;
- reclassify the first result as
  `rb5-lab1-observation-v1-20260806T144001Z-6544a277.partial` with
  `state=failed` and `transport_messages_lost=14`.

The exact ROS 2 Jazzy warning is retained as the regression fixture
`src/sang_mcap_recorder/test/fixtures/rosbag2_jazzy_transport_loss.log`.

## Passing Session

- Session: `rb5-lab1-observation-v1-20260806T144401Z-814499ab`
- Location:
  `/home/lab1/sang/runtime/processing-lab1/data/captures/`
- MCAP duration: 9.120368690 seconds
- MCAP size: 69.8 MiB
- Messages: 678 total
- ROS bag reported transport loss: 0
- Zero source-header timestamps: 0 for every topic
- Source timestamp regressions: 0 for every topic
- Independent SHA-256 recalculation: all listed files matched
- Existing Agent, cameras, RB5 state adapter, and network containers: running,
  restart count 0 before and after capture

| Source | Messages | Approx. source-header rate |
|---|---:|---:|
| RB5 measured joints | 204 | 30.0001 Hz |
| OTOS welding view | 200 | 21.8887 Hz |
| D455 color | 274 | 29.9637 Hz |

`ros2 bag info` independently reported the expected three topics and types:

- `/rbpodo/joint_states` - `sensor_msgs/msg/JointState`
- `/rb5_lab1_eval_01/camera/welding_view/image_raw/compressed` -
  `sensor_msgs/msg/CompressedImage`
- `/rb5_lab1_eval_01/camera/d455_color/image_raw/compressed` -
  `sensor_msgs/msg/CompressedImage`

## Verdict And Open Quality Debt

**PASS for the Phase 3.5 local storage integrity MVP.** A complete session now
means that the allowlisted topics and types are present, every topic is
non-empty, source headers are non-zero and non-regressing, ROS bag reported no
transport loss, and all artifact hashes were generated and independently
verified.

This is **not a training-data quality approval**. The OTOS rate was below the
25 Hz observation target, consistent with the open performance issue from the
two-hour sync soak. Clock quality also remains `UNVERIFIED` because no PTP/NTP
offset evidence is collected by the recorder. Rate/gap/skew acceptance remains
the responsibility of `sensor_sync_probe` until a versioned capture quality
gate is added.

The one-time Claude Fable Phase 3.5 review returned **PASS with zero critical
issues**. Its focused hardening findings were applied: literal symlink
rejection, file/manifest/directory `fsync`, failure-manifest creation
independent of log preservation, the exact Jazzy loss-warning regression
fixture, fake-process lifecycle tests, and narrower read-only requirements
wording. The final package build and repository suite passed with 248 tests.
