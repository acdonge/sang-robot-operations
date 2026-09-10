# PLAN - SANG documentation consolidation

Goal: Merge the repository's implementation plans into one current roadmap, preserve detailed design
and dated evidence as traceable supporting records, and correct statements that conflict with the
implemented code or the 2026-09-01 OPC UA Robotics/AAS audit.

Done when:

- one master implementation plan is the sole authority for current status, next work, and deferrals;
- one document index classifies every root `PLAN_*.md` and every first-party `docs/*.md` record;
- README, requirements, and the major architecture documents state their authority and current status;
- known false or stale claims about Central OPC UA, G1-G6a, Machine Vision, and AAS are corrected;
- existing evidence and completed plan records remain intact and discoverable;
- Markdown links and repository consistency checks pass; and
- the earlier independent Claude standards critique is incorporated, and any fresh re-review attempt
  or external limitation is recorded without inventing a verdict.

Out of scope:

- runtime behavior changes, database migrations, or OPC UA address-space changes;
- enabling physical robot, EEF, welder, feeder, or vendor-controller commands;
- implementing the official OPC UA Robotics projection, a real AAS Repository, or Dynamic Unity A1;
- rewriting dated evidence to match later implementation state.

## Milestones

- [x] M1: Inventory plans, designs, runbooks, and evidence; identify contradictions.
- [x] M2: Define document authority, precedence, and preservation rules.
- [x] M3: Write the canonical master implementation plan and document authority index.
- [x] M4: Correct status and cross-reference drift in the core documents.
- [x] M5: Run independent critique, link checks, diff review, and consistency verification.

## Decisions log

- 2026-09-01: Do not concatenate or delete historical files. Their dates, hashes, and acceptance
  boundaries are evidence and must remain stable.
- 2026-09-01: Use a master-plan plus index model. The master owns status and sequence; subsystem
  designs own detailed contracts; dated evidence proves only the tested snapshot.
- 2026-09-01: Treat the custom SANG OPC UA v1 model as implemented compatibility behavior, not as an
  OPC UA Robotics 1.02-conformant server.
- 2026-09-01: Treat A0 as internal AAS-ready contracts only. Pin a current IDTA specification release
  before implementing an AAS projection or repository.
- 2026-09-01: Keep raw D405/D455/OTOS/MJPEG cameras outside OPC UA Machine Vision. Apply that Companion
  Specification only if a complete vision-processing system with jobs/configuration/results is built.
- 2026-09-01: A fresh Claude Opus consolidation review could not complete because the first run did not
  return a report and the bounded retry hit the account session limit. Do not claim a fresh PASS; rely
  on the prior two-round audit plus recorded local coverage/link/code checks for this iteration.

## Current state

All milestones are complete. The master plan separates accepted observation/data/simulation behavior,
software-complete orchestration, contract-only AAS work, and unimplemented physical/process control.
The authority index preserves every first-party plan and evidence record. Core status conflicts were
corrected, all local links resolve, and the validation record explicitly distinguishes the earlier
Claude audit from the fresh review attempt that hit the account session limit.
