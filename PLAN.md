# PLAN — M4-⑥ recording UX parity

Goal: Extend the V2 recorder from one-episode datasets to durable multi-episode sessions with episode controls, rotating instructions, reset flow, and system status.

Done when: The writer keeps one dataset open until finish; FSM/gateway support save-next, retry, finish, continue, automatic episode limits; session metadata/status expose the requested fields; focused and regression tests pass; and `smoke_m4_6.sh` encodes the requested two-episode workflow plus M4-3/M4-5 regressions.

Out of scope: Updating `ui/index.html`, running Docker, committing changes, or executing hardware/ROS smoke tests.

## Milestones

- [x] M1: Current contracts and LeRobot API constraints documented — verify by: inspect writer/FSM/gateway/tests and installed LeRobot API.
- [x] M2: Multi-episode writer/session metadata implemented — verify by: writer tests covering save/retry/finalize semantics.
- [x] M3: FSM and gateway controls/status implemented — verify by: focused FSM/gateway tests for transitions, retry, rotation, and auto-finish.
- [x] M4: M4-⑥ smoke workflow added with requested regressions — verify by: shell syntax check and contract review.
- [x] M5: Full requested static/unit verification and diff review — verify by: `py_compile`, `pytest`, and fresh diff audit.

## Decisions log

- 2026-07-14/intake: Preserve the M4-⑤ architecture and avoid UI changes, Docker, commits, and ROS smoke execution per request.
- 2026-07-14/M1: Use pinned LeRobot's public `clear_episode_buffer()` for retry. Delayed recording would retain decoded 5-camera RGB frames in Python RAM; because the pinned synchronous image path leaves PNGs behind, the writer also removes only that unsaved episode's staging image directories after the public clear call.
- 2026-07-14/M1: Keep `/record/stop` as a backward-compatible alias of finish; `num_episodes=0` and `episode_time_s=0` mean unlimited/disabled so M4-⑤ callers remain valid.
- 2026-07-14/M3: Episode indexes are zero-based to match LeRobot metadata. Retry records an attempt with the same index and does not rotate instructions; only `SAVED` advances the index/instruction.
- 2026-07-14/M3: The requested `rosbag` flag is persisted as session configuration; this milestone does not introduce a rosbag process because the existing recorder has no rosbag lifecycle boundary.

## Current state

All milestones complete. Fresh verification passes: 47 tests, all Python files compile, all smoke scripts parse, and the diff whitespace check is clean. ROS smoke execution remains intentionally delegated to Claude per request.
