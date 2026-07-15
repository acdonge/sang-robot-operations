# PLAN — M4-⑨ closed-loop inference manager

Goal: Load the final pinned LeRobot v0.6 checkpoint and execute it against fake_robot observations at manifest FPS with strict schema and freshness safety gates.

Done when: DONE runs with a real final checkpoint are listed as policies; inference start synchronously loads and preflights exact state/camera/action keys and shapes; one closed loop converts JPEG observations, calls the policy, and publishes leader JointTrajectory commands until idempotent stop or stale/error shutdown; gateway REST/WebSocket contracts and fake_robot-only warnings are present; focused/full tests and static checks pass.

Out of scope: UI changes, real-robot enablement, M7 bridge/deadman implementation, Docker execution, commits, and changes to the trained policy architecture.

## Milestones

- [x] M1: Inference manager and ROS observation/action boundary implemented — verify by: mismatch, transition, closed-loop publish, stale-stop, idempotency, and checkpoint-selection unit tests.
- [x] M2: Gateway policy/start/stop/WebSocket contracts implemented — verify by: focused route tests for DONE filtering, recording/concurrency 409, warning, idempotency, and inference status.
- [x] M3: M4-⑨ smoke workflow and safety documentation implemented — verify by: shell syntax, executable bit, and contract review.
- [x] M4: Fresh full verification and skeptical diff review — verify by: `py_compile`, full `pytest`, smoke syntax, and `git diff --check`.

## Decisions log

- 2026-07-15/intake: Treat only IDLE/READY recorder nodes as inference-startable; this covers the required recording conflict and prevents policy commands during warmup/commit phases.
- 2026-07-15/API: Load the final `checkpoints/last/pretrained_model` through pinned v0.6 public `PreTrainedConfig.from_pretrained` plus the registered policy class `from_pretrained`, and restore the saved public pre/post processor pipelines.
- 2026-07-15/schema: Compare policy-space visual shapes as `(3, height, width)` because pinned `dataset_to_policy_features` converts recorded HWC video features to channel-first tensors.
- 2026-07-15/safety: A freshness failure terminates the command loop in `ERROR`; explicit stop transitions any terminal state to `STOPPED`. The executor remains fake_robot/sim-only and does not claim M7 real-robot safety.

## Current state

All milestones complete. Fresh verification passes: 74 tests, all workspace Python compilation, all M4 smoke shell syntax, M4-⑨ executable bit, and the diff whitespace check. The ROS/LeRobot end-to-end smoke is encoded but was not executed because Docker is prohibited and the host has neither ROS nor LeRobot.
