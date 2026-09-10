# GitHub Source Publication Policy

Audit date: 2026-09-10. Baseline: local HEAD `798ac63`, 18 reachable commits.
This is a source-publication boundary, not permission to publish, an asset license,
or a claim that a clean clone reproduces the live workcell. Repository destination,
staging, commits, and upload remain the publication owner's responsibility. The
user-approved destination is `acdonge/sang-robot-operations`, with **PRIVATE**
visibility; no public release or asset redistribution permission is implied.
This audit changed only this document and the root `.gitignore`; it did not delete
files, modify the index/history, contact GitHub, or touch running services.

## Included Source

- `src/`, `scripts/`, `docker/`, `deploy/`, `manifests/`, and `config/`: application,
  tests, deployment recipes, topic contracts, calibration/configuration, and build
  roles. Lab addresses, device identifiers, and workstation paths are not portable
  defaults and require owner review before public disclosure or use elsewhere.
- `contracts/`: JSON Schemas, examples, task/adapter profiles, OPC UA mappings,
  `contracts/opcua/official/*.xml` and `nodesets.lock.yaml`, and
  `contracts/aas/official/aas-3.1.2.schema.json` and `metamodel.lock.yaml`. Preserve
  exact bytes, pinned hashes, provenance, and license notices together.
- `frontend/admin/`: hand-authored frontend, `package.json`, `package-lock.json`,
  TypeScript/Vite configuration, and build scripts; not dependency/build caches.
- `sim/`: Python source and pinned JSON data. In particular, keep
  `sim/isaac_scenario1/{manifest,layout,motion,omy_model}.json`. Although some data
  was extracted/generated, it is required by the existing hash-checked contracts,
  not disposable runtime output. Its external-source provenance still needs review.
- `unity/Sang.Observer/`: package source, assembly definitions, tests, and `.meta`.
  For both `unity/IsaacRb5Observer/` and `unity/IsaacScenario1Observer/`, retain
  `Assets/Observer/Runtime/`, `Assets/Observer/Editor/`, their source `.meta` files,
  `Packages/manifest.json`, `Packages/packages-lock.json`, `ProjectSettings/`,
  `build-profile.json`, launchers, tests, and the Scenario 1 layout extractor.
  Audited project/organization/cloud identity fields are empty; recheck before upload.
- Markdown documentation and source SVG diagrams. Environment `.env.example` and
  `.env.template` files remain eligible only with non-secret example values.
  The checked-in recorder fixture `src/sang_mcap_recorder/test/fixtures/rosbag2_jazzy_transport_loss.log`
  is explicitly preserved despite the general log exclusion.

No blanket `*.json`, `*.yaml`, `*.xml`, `*.lock`, `*.asset`, or `*.meta` rule is used.
Eligibility is not a provenance, safety, or license approval.

## Kept Local

| Path or rule | Reason / reproduction boundary |
| --- | --- |
| `runtime/`, `output/`, `store/`, `/var/` | PKI, identities, credentials, SQLite state/backups, captures, derived objects, telemetry, reports, logs and caches. Preserve existing evidence locally, including `output/validation/`; do not replace it with invented clean-clone evidence. |
| `/hardware/` | Separate `hardware/rb5_cwm2312_adapter/` work is outside this SANG publication scope. This does not exclude the SANG read-only adapter packages under `src/`. |
| `/aasx-package-explorer*` | Both extracted `2024-06-10` tool copies, disabled plugins, and the ZIP download. Optional external viewer, not application source or a required vendored dependency. |
| `/ui-admin/`, `/ui-operator/` | Generated deployment output; rebuild from `frontend/admin/`. Already-tracked files require a separate index decision below. |
| `/.agents/`, `/.codex/config.toml`, `/.claude/launch.json` | Machine/assistant configuration, not portable application source. The latter two are already tracked. |
| `build/`, `install/`, `log/`, `.venv/`, `node_modules/`, `dist/`, Python/test/typecheck caches, `*.tsbuildinfo`, coverage output | Recreate in the intended development environment. Existing nested Unity ignore files remain unchanged. |
| `unity/*/{Library,Temp,Obj,Logs,UserSettings,Build,Builds,MemoryCaptures,Recordings}/` and case variants in `.gitignore` | Editor imports/caches, user state, Linux Players, assemblies, and build evidence; regenerate rather than upload. |
| `unity/*/Assets/Observer/Visuals/` and `Visuals.meta` | Imported RB5 DAE, OMY prefab/mesh assets, welding-table OBJ, derived materials, and all associated vendor/import metadata. Ownership/redistribution evidence is incomplete. |
| `unity/*/Assets/Observer/Scenes/` and `Scenes.meta` | Generated scenes derived from the external hierarchy and visual assets. Recreate through the checked-in Editor build methods, not by opening a missing-assets scene and saving it. |
| `.env`, `.env.*`, `*.env` except documented example/template names; key/keystore extensions; SSH private-key names; `*-password.txt`, `*-token.txt`, `*.secret` | Never publish operational credentials. Re-enroll locally rather than reuse someone else's keys. An extension rule is not a content scanner. |
| SQLite `*.db`, `*.db3`, `*.sqlite`, `*.sqlite3` plus sidecars; `*.mcap`, `*.mcap.partial`, `*.bag`, `*.bag.active` | Operational state and recordings, not source fixtures. Keep failed/partial evidence locally. |
| Compiled artifacts, archives, raster/audio/video files, and PDF extensions listed in `.gitignore` | Default source-only policy, including `SANG_MASTER_IMPLEMENTATION_PLAN.pdf`. Markdown/SVG source remains eligible. Any deliberate media exception needs rights/privacy review and a narrow rule. |

These rules protect ordinary adds of **untracked** files only. They neither remove
tracked files nor prevent a forced add, and they do not sanitize commit history.

## Reproduction Requirements

1. Use the ROS 2 Jazzy environment and build/test instructions in `README.md` and
   `docs/requirements.md`. Install dependencies from project recipes, not copied
   local environments. Rebuild frontend deployment output with `npm ci` followed
   by `npm run build` from `frontend/admin/`; ensure `ui-admin/` and `ui-operator/`
   directories exist in a source-only checkout because the sync script copies to
   the latter. Rebuild before serving or packaging the UI.
2. Generate new runtime PKI, local credentials, registry state and environment files
   through `deploy/phase3/`, `deploy/phase4/`, and
   `docs/ROBOT_CAMERA_OPERATIONS_V1.md`. Do not distribute CA private keys, existing
   passwords, recordings, or a lab database as a bootstrap shortcut.
3. Obtain Unity Editor `6000.5.6f1` with Linux build support and separately authorized
   external assets. The build scripts currently reference
   `/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app`;
   the Isaac profiles also require the external `issacsim_setting_usd` tree.
   Keep the original Unity scene, meshes, metadata, trajectories and consumed URDF
   importer sources at the expected paths and versions. These are **not included**
   by cloning this repository. There is no complete public acquisition/license
   manifest for this external bundle.
4. The Scenario 1 manifest pins 136 sources: 12 inside this repository and 124
   external files, including original scene/visuals, trajectories, USD inputs, and
   two URDF-importer `Library/PackageCache` sources. All matched during this audit.
   Keep all 12 internal sources eligible and all pinned bytes unchanged. A clean
   clone cannot pass full source verification until the authorized external bundle
   is supplied. Do not delete manifest rows, loosen hash checks, or silently update
   pinned data to make a missing dependency pass.
5. After obtaining the exact inputs, use `extract_layout.py --check` and the
   `Sang.IsaacRb5Observer.Editor.IsaacRb5ObserverBuild.BuildLinux` /
   `Sang.IsaacScenario1Observer.Editor.IsaacScenario1ObserverBuild.BuildLinux`
   methods documented in `docs/ISAAC_RB5_OBSERVER_BUILD_2026-09-09.md` and
   `docs/ISAAC_SCENARIO1_OBSERVER_2026-09-09.md`. They reconstruct scenes/visuals
   before building and audit the resulting application assemblies. Recreate local
   build evidence rather than pretending historical Player hashes describe a new
   build. Path changes, new dependency versions, or repinning need a separate review.

Reproduction instructions are not authorization to launch live services, Players,
hardware, or physical outputs. This audit ran no Unity/Isaac build or live action.

## Provenance And License Gaps

- The local copies of all four official OPC UA NodeSets carry embedded OPC
  Foundation MIT License 1.00 notices. Preserve those notices and the source lock.
  This is file-level provenance, not a Robotics conformance claim.
- `contracts/aas/README.md` and `metamodel.lock.yaml` identify IDTA, revision
  3.1.2, original source, and CC-BY-4.0. Preserve that attribution with the unchanged
  schema; publication owner should verify complete license/attribution packaging.
- No root first-party `LICENSE` was found in the audited workspace. The owner must
  decide the intended license and confirm contributor rights; this audit does not
  invent or apply an open-source license.
- No asset-specific redistribution/license bundle was found beside the imported
  Unity RB5, OMY, welding-table, and derived scene/visual copies. An upstream robot
  code license or a locally available vendor file is not recorded permission for
  this exact model/texture/scene bundle. Keep the copies local until the owner
  records the supplier, exact revision/hash, rights holder, permitted uses,
  attribution/notices, and any redistribution restrictions. Review provenance of
  derived `layout.json`, `motion.json`, and `omy_model.json` too; required manifest
  data is retained, but retaining it is not license clearance.
- Unity/Isaac runtimes and downloaded AASX Explorer/plugin binaries are separately
  supplied dependencies. This source policy does not relicense or redistribute them.

## Audit Findings And Publication Gate

The read-only audit scanned 4,342 existing tracked/default-add candidate files
before exclusions, with no file-read errors, and all 555 reachable historical
blobs (about 5 MB), plus all 18 reachable commit objects including messages and
two reachable annotated tag objects. It checked PEM private-key blocks, GitHub/Hugging Face/OpenAI/
Slack/AWS token signatures, JWTs, credential-bearing URLs, literal secret-like
assignments, SQLite headers/paths, symlinks, and file sizes. Ignored runtime/output
directories received a sensitive-filename inventory only; live databases were not
opened and private-key/password values were not printed. Compressed archive
members, encoded secrets and unreachable/reflog-only
objects were not exhaustively scanned. Neither Gitleaks nor TruffleHog was installed.

- No private-key block, provider-token signature, JWT, or SQLite blob/path was found
  in the scanned candidate content or reachable history. No candidate symlinks were
  found after exclusions. This is a limited scan result, not a clean-secret guarantee.
- Historical generic literal-secret review matches occurred in
  `src/fleet_control_plane/test/test_auth.py`, `test_capture_jobs.py`, and
  `test_recording_storage.py`. Current-tree matches additionally occurred in
  `test_commissioning_evidence.py`, `test_workflow_authoring.py`, and the
  credentialed-URL rule in `test_unity_runtime_proxy.py`. These test-context hits
  require release review; no production credential was confirmed or reproduced.
  The two downloaded `libcef.dll` copies also matched the URL heuristic and are
  excluded wholesale; their binary contents are not source-publication candidates.
- Existing `runtime/` and `output/` contain database/sidecar and PKI/environment/
  password paths. `/var/` contains simulation evidence and caches. They stay local.
- Reachable history's largest blob is
  `docs/SANG_CURRENT_AND_FUTURE_ARCHITECTURE_KO.png` (330,699 bytes); no historical
  blob exceeds 10 MiB. Before exclusions, the AASX ZIP was 355,830,634 bytes and
  each downloaded `libcef.dll` was 199,268,352 bytes. None belongs in source adds.
- **Already tracked despite new exclusions:** `.codex/config.toml`,
  `.claude/launch.json`, the architecture PNG above, and 15 generated files under
  `ui-admin/` and `ui-operator/`. Their history survives these rules. The publication
  owner must explicitly decide whether to retain reviewed exceptions or separately
  remove them from the index while preserving local files. A source-only UI must
  be rebuilt before use. Do not ship updated tracked HTML without its matching
  generated chunks. This audit performed neither action nor a history rewrite.

Before any upload, the publication owner should review
`git ls-files --cached --ignored --exclude-standard`, inspect the proposed staged paths, rerun a redacted
secret scan over the exact staged content and every ref selected for publication,
and resolve provenance/visibility questions. If a real secret is identified,
stop publication and arrange revocation/rotation plus an explicitly authorized
history decision. Do not assume deleting the current file removes the old value.

### Publication Owner Decision (2026-09-10)

The approved destination is the newly created private repository
`acdonge/sang-robot-operations`. The current checkpoint removes the generated
`ui-admin/`, `ui-operator/`, `.codex/config.toml`, and `.claude/launch.json` from
Git tracking only, preserving any existing local files and all prior history.
README documents the source-only frontend build prerequisite. The existing
project architecture PNG is retained as a reviewed documentation exception;
new recording media and external visual assets remain excluded. No root license
or public redistribution grant is added. See `GITHUB_CHECKPOINT_2026-09-10.md`
for publication checks and remaining qualification limits.
