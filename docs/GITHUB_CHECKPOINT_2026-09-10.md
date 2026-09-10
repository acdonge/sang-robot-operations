# GitHub Source Checkpoint - 2026-09-10

## Purpose And Scope

User-approved destination: [acdonge/sang-robot-operations](https://github.com/acdonge/sang-robot-operations),
**private**, retaining the existing local `master` history. This is a development
source checkpoint, not a production release, physical commissioning, OPC UA
certification, or permission to redistribute third-party assets publicly.

The [platform overview](SANG_PLATFORM_OVERVIEW_KO.md) records the agreed final
product and current implementation. The [master plan](../SANG_MASTER_IMPLEMENTATION_PLAN.md)
remains the roadmap authority. Historical validation documents retain their dates.

## Included And Excluded

Includes the existing SANG backend, frontend source, schemas and official NodeSet
locks, observation/recording adapters, orchestration and simulation contracts,
Isaac executors, first-party Unity observer source, tests and implementation docs.

The exact staged tree initially contained 740 files / 11,710,731 bytes before this
report. Its largest file was the official Core NodeSet (4,030,444 bytes). No staged
runtime databases, captures, keys, installers, external visual assets, Unity
Players or caches were found. Generated Web output and two machine-specific
assistant configurations are removed from tracking only; local files and prior
history remain. The existing architecture PNG is a retained documentation asset.

See [source publication policy](GITHUB_SOURCE_POLICY.md) for exclusions, licenses
and reconstruction requirements. A clone still needs separately authorized
Unity/vendor assets: 124 of the 136 pinned Scenario 1 input files are external.
Frontend deployment files must be built from `frontend/admin/` before serving.

## Fresh Checks

| Check | Result and scope |
| --- | --- |
| Architecture/status cross-review | PASS, independent read-only review of the overview and canonical status corrections |
| Source-publication audit | PASS for reviewed scope; 555 historical blobs, 18 commits, two tag objects; no confirmed production secret |
| Exact staged-tree signature scan | No private-key, provider-token, JWT or SQLite signatures; no forbidden runtime/media paths or symlinks |
| Isaac twin + Scenario 1 Python tests | 400 passed; no simulator or hardware launched |
| ROS/backend/security/vendor-profile source tests | 754 passed, 14 dependency deprecation warnings; isolated existing Jazzy image, no host networking or device mounts |
| Source-only frontend reconstruction | Typecheck and build passed from an exported Git index in a temporary directory, reusing the installed dependency cache; not a fresh network dependency installation |
| Frontend selection tests | 13 passed on the exported source |
| Rebuilt UI tests | 15 passed on the exported/rebuilt source tree, with one dependency warning; these overlap the broad backend selection |
| Local documentation links | 162 links across eight entry/canonical documents resolved within the publish tree |
| Documentation whitespace | PASS for this task's documents and ignore rules, separately from pre-existing generated/vendor formatting |

The first broad container attempt returned 749 passes and five failures caused by
the test harness: executable fixtures on a non-executable temporary mount, an
unwritable ROS logging directory, and missing read-only external USD fixtures.
The same complete 754-test selection passed after correcting those environment
settings. Application code and pinned data were not modified to suppress errors.
The frontend build retains a warning about a minified shared chunk above 500 kB;
bundle splitting remains a performance follow-up, not a failed build.
The earlier host-only collection attempt lacked ROS and other dependencies; it
is not evidence of a passing full host test suite.

Whole-checkpoint whitespace checking reports existing trailing whitespace in
official XML/Unity-generated metadata and two pre-existing extra EOF blank lines.
Those bytes are preserved rather than formatting pinned dependencies or adjacent
runtime source as part of publication. This is not an unqualified repository-wide
whitespace PASS.

The limited secret checks do not prove that every encoded value or archive is
secret-free. Ignored runtime contents and unreferenced Git objects are not being
published. No history rewrite, dependency download, live deployment, simulator
run, physical command, or new control authorization was performed in this task.

## Important Remaining Work

- `ACT-C01`: Action Library publication must bind successful historical evidence
  to the current adapter/profile digest; registered definitions are not installed
  build attestations. This was documented, not fixed during publication.
- Official Robotics projection still needs complete units/ranges, qualified real
  profiles and independent client interoperability evidence. No conformance claim.
- The latest Isaac Scenario 1 success is a fixed local virtual fixture, not generic
  distributed worker deployment or qualified grasp/contact/mobile dynamics.
- Original OMY repetition, scene/visual alignment, signed package delivery,
  physical safety commissioning, XR intervention and VLA policy execution remain
  distinct follow-up work. Software HELD is not a physical E-stop.

## Publication Status

**PASS for private source publication.** The source checkpoint was pushed to
`origin/master`, and a fresh `git ls-remote` result matched the local commit:

`ef50bbc29e9222bb0e3df4ff9ba08c150c6c199b`

GitHub independently reports `visibility: private` and default branch `master`.
This report and the completed plan are a follow-up documentation commit; the
hash above identifies the source checkpoint they verify, not their own commit.
The source tree contains 741 files including this report. No force push, history
rewrite, live service restart or hardware operation was performed.
