# PLAN — M4-⑧ native LeRobot training manager

Goal: Run and monitor pinned LeRobot v0.6 ACT training in an isolated subprocess with immutable lineage, safe stop semantics, and gateway REST/WebSocket contracts.

Done when: A validated indexed dataset can start one CPU ACT run under `store/runs/<run_id>`, publish PREPARING/RUNNING/progress/terminal events, stop via SIGTERM then SIGKILL fallback, report DONE only when a real checkpoint exists, and all focused/static/full tests pass.

Out of scope: Resume, UI changes, GPU training, Docker execution, commits, and real long-running training in this host environment.

## Milestones

- [x] M1: TrainManager and lineage implemented — verify by: subprocess-mocked command, progress, success/error, concurrency, and stop tests.
- [x] M2: Gateway REST and WebSocket integration implemented — verify by: focused route/event tests including 404/409 behavior.
- [x] M3: Container smoke workflow and documentation implemented — verify by: shell syntax and contract review.
- [x] M4: Full static/unit verification and diff review — verify by: `py_compile`, `pytest`, smoke syntax, and `git diff --check`.

## Decisions log

- 2026-07-14/intake: Use the v0.6 official `lerobot-train` console entry point from the pinned SHA; do not import the training pipeline into the gateway process.
- 2026-07-14/intake: Keep immutable `lineage.json` and `train.log` at the run root while passing the initially absent `run/output` child to LeRobot, because v0.6 rejects a pre-existing `output_dir` for non-resume runs.
- 2026-07-14/intake: Force a small, offline-safe ACT configuration on CPU (batch 1, reduced transformer, no pretrained backbone download, no Hub/W&B) so the required 20-step smoke is bounded.
- 2026-07-14/intake: A zero exit code without checkpoint files is ERROR, and an operator-stopped run ends ERROR after STOPPING; interruption is never reported as successful completion.
- 2026-07-14/scope: Resume is deliberately absent so no checkpoint config can be modified in place.

## Current state

All milestones complete. Fresh verification passes: all source Python files compile, 67 tests pass, every M4 smoke script parses, `smoke_m4_8.sh` is executable, and the diff whitespace check is clean. Docker/container smoke execution was intentionally not performed because the task prohibits Docker and the host interpreter has no LeRobot installation.
