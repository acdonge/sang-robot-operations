# PLAN — M4-⑦ dataset browser and Hugging Face upload

Goal: Add a read-only dataset index and a safe asynchronous Hugging Face dataset upload path to the existing recorder gateway.

Done when: Dataset metadata is listed from the fixed store layout; validated promoted datasets can be uploaded through an injectable worker with observable state, cancellation, and session lineage; REST and WebSocket contracts expose the feature; focused tests, Python compilation, pytest, and smoke-script syntax checks pass.

Out of scope: Dataset deletion, UI changes, Docker execution, real Hugging Face network calls, commits, or changes to the pinned LeRobot image unless the dependency is absent.

## Milestones

- [x] M1: Store index and fixtures implemented — verify by: focused unit tests over a temporary store.
- [x] M2: Injectable upload worker implemented — verify by: mocked success, cancellation, preflight, and failure tests with no network.
- [x] M3: Gateway REST and WebSocket integration implemented — verify by: focused gateway route/event tests.
- [x] M4: M4-⑦ smoke workflow encoded — verify by: shell syntax and contract review without Docker or HF upload.
- [x] M5: Full static/unit verification and diff review — verify by: `py_compile`, `pytest`, smoke syntax, and `git diff --check`.

## Decisions log

- 2026-07-14/intake: Derive the store root from each manifest's fixed `store/datasets/raw` path and preserve the existing `create_app` injection style for tests.
- 2026-07-14/intake: Implement cancellation on the upload manager only; no HTTP DELETE/cancel route is added because it is outside the specified gateway contract.
- 2026-07-14/intake: Use stage events (`preparing`, `uploading`, terminal state) because `HfApi.upload_folder` does not expose a stable public byte-progress callback in the installed version.
- 2026-07-14/intake: Keep the token only in the request model and worker call stack, redact it from exception details, and never persist or log it.
- 2026-07-14/dependency: `huggingface_hub 0.35.3` is already installed through the pinned LeRobot environment, so Dockerfile changes are unnecessary.

## Current state

All milestones complete. Fresh verification passes: all Python files compile, 58 tests pass, all M4 smoke scripts parse, the diff whitespace check is clean, and no DELETE dataset route exists. Docker, ROS smoke execution, commits, and real HF network calls were intentionally not performed.
