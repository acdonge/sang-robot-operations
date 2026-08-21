# PLAN - Admin continuity and robot conformance

Goal: Keep the Admin UI visually stable during live refreshes and add read-only monitoring
contracts for the lab's OMY and RB5-850E robots.

Done when:
- Polling, WebSocket, and manual refreshes reuse existing Agent/detail DOM nodes and update
  values without replaying entrance animations.
- OMY and RB5 manifests load through the same Robot Agent parser as SO-101.
- Demo snapshots use the observed upstream ROS 2 topics for all three robot families.
- Focused tests and an in-app browser check pass with no application console errors.

Out of scope:
- Starting OMY or RB5 hardware, sending robot commands, or changing control modes.
- Adding RB5 cameras/EEF before their physical configuration is known.
- Remote Agent transport, TLS/mTLS, command leases, recording, or policy execution.

## Milestones
- [x] M1: Add keyed in-place DOM reconciliation - verify by: automatic refresh preserves
  the selected Agent row and does not replay row/inspector entrance animations.
- [x] M2: Add OMY and RB5 read-only manifests - verify by: parser tests cover source topics,
  required health inputs, and minimum-rate contracts where upstream specifies a rate.
- [x] M3: Align seed/docs and run end-to-end verification - verify by: all focused tests pass
  and the browser renders SO-101, OMY, and RB5 with a clean console.

## Decisions log
- 2026-07-22: Reconcile generated DOM instead of merely disabling CSS animations so focus,
  node identity, and future operator interactions survive live updates.
- 2026-07-22: OMY topics follow the locally used ROBOTIS `omy_f3m_config.yaml` contract:
  `/joint_states`, `/leader/joint_trajectory`, and the `cam_wrist` compressed image topic.
- 2026-07-22: RB5 observes `/rbpodo/joint_states`, the raw 30 Hz ros2_control broadcaster
  remap in Rainbow Robotics' official bringup. No command source or EEF is declared yet.

## Current state
M1-M3 are complete. The full 45-test suite and both package builds pass. Browser verification
confirmed that the live timestamp changes while SO-ARM row focus, selection, and inspector identity
remain stable; manual refresh also keeps both row and inspector fully opaque without replaying their
entrance transforms. SO-101 observability and physical soak/reconnect verification remain complete.
