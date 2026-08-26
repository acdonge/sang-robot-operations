# PLAN - MCAP Web Playback

Goal: Let a user select a completed recording session and play up to four recorded camera topics together on one synchronized web timeline without Foxglove or a full MCAP browser download.

Done when:

- Central exposes only completed, verified `sensor_msgs/msg/CompressedImage` topics from the immutable Object Store;
- frame lookup uses the MCAP summary/index and returns the nearest JPEG for a bounded timeline offset without loading the whole file into memory;
- Data Catalog supports session selection, one-to-four camera selection, play/pause, seeking and playback speed on a shared timeline;
- each view reports its actual frame time/skew so sparse or stale cameras are visible rather than silently presented as synchronized;
- the existing viewer authorization, immutable object, and zero robot/welding command boundaries remain intact;
- automated tests and a real browser check pass against the current RB5 recording.

Out of scope: MP4/HLS generation, audio, image decode/re-encode, depth-image visualization, arbitrary ROS message decoding, frame-accurate hardware synchronization, annotations, dataset clipping, and actuator commands.

## Milestones

- [x] M1: Add bounded MCAP playback reader and tests - verify with synthetic and current RB5 MCAP frames.
- [x] M2: Add read-only playback metadata/frame APIs and authorization/route tests.
- [x] M3: Add multi-camera Catalog player and responsive styling with browser tests.
- [x] M4: Rebuild/deploy Central, run end-to-end playback against current sessions, and document evidence.

## Decisions Log

- 2026-08-24: Use official `mcap==1.4.0`, whose seeking reader filters by topic and nanosecond time using the MCAP summary/index. Do not download or parse the full object in the browser.
- 2026-08-24: Use a 100 ms shared record-time bucket (10 fps UI) and return the actual source timestamp/skew with every frame. This bounds load and makes sparse cameras explicit.
- 2026-08-24: Decode only ROS 2 CDR `sensor_msgs/msg/CompressedImage` and pass JPEG bytes through unchanged. Other message/image types remain unavailable rather than being guessed.
- 2026-08-24: Limit one browser to four simultaneous camera views. This covers the current three-camera RB5 session while bounding Central disk and network load.

## Current State

M1-M4 are complete. The deployed Catalog selects a complete session, shows up to four camera topics,
and supports play/pause, timeline seeking and 0.5x/1x/2x playback on one record-time clock. The current
RB5 session rendered D405, D455 and OTOS together; at 10.0 seconds D405 and OTOS were within +5/-1 ms,
while the two-frame D455 source was visibly marked at -2941 ms. Browser console errors were zero and
the acceptance evidence is recorded in `docs/MCAP_WEB_PLAYBACK_ACCEPTANCE_2026-08-24.md`.
