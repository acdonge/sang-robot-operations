# MCAP Web Playback Acceptance - 2026-08-24

## Scope

Validate read-only, indexed web playback of completed camera observations without Foxglove, full-file
browser downloads, image re-encoding, or any actuator command path.

## Deployed target

- Central: `http://127.0.0.1:18091`
- Task Run: `rb5-ui-capture-acceptance-20260824`
- Session: `capture-2693b1f247ea4ff28a4331ad1ffb2ca3`
- Immutable MCAP: 127.1 MiB, SHA-256 verified before index construction
- Cameras: D405 (565 frames), D455 (2 frames), OTOS WGC200 (575 frames)

## Evidence

- Catalog opened all three selected cameras simultaneously and returned three JPEG views.
- Play/pause advanced the shared record-time clock; 2x speed and rewind worked.
- Timeline seeking to 10.0 seconds updated all selected views.
- At 10.0 seconds, D405 skew was +5 ms and OTOS skew was -1 ms.
- D455 skew was -2941 ms because this recording contains only two D455 frames; the UI marked it stale
  instead of presenting false synchronization.
- Deselecting D455 reduced the grid and requests to D405 plus OTOS; selecting it restored three views.
- Warm frame endpoint measurements were 1.49 ms (D405), 1.68 ms (D455), and 1.83 ms (OTOS).
- Browser console reported zero errors and zero warnings.
- Full related regression suite passed: 286 tests across Fleet Control Plane, Robot Agent, and MCAP
  Recorder (one upstream Starlette/httpx deprecation warning).

## Safety and load boundaries

- Only complete Catalog sessions and complete `.mcap` objects are eligible.
- Object size and SHA-256 are verified before an index is cached; file identity changes invalidate it.
- The reader uses MCAP summary indexes and bounded nearest-frame reads rather than loading the object.
- Only ROS 2 CDR `sensor_msgs/msg/CompressedImage` containing complete JPEG data is accepted.
- One browser selects at most four cameras; read slots, record size, frame size, index cache, and frame
  cache are bounded.
- Playback endpoints are GET-only. No robot, vendor, policy, welder, or feeder command route was added.

## Verdict

PASS. The requested session selection, multi-camera selection, synchronized timeline playback, seeking,
and speed controls are operational. D455 remains a source-recording quality issue, not a player defect.
