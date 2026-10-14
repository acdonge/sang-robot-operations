# GitHub Source Publication Policy

This public repository contains application source, deployment examples, user guides
and versioned interface contracts. Internal plans, assistant instructions, design
reviews and dated experiment records are maintained in a separate private repository.

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
- Public user guides, requirements, interface documentation and source SVG diagrams. Environment `.env.example` and
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
| `/ui-admin/`, `/ui-operator/` | Generated deployment output; rebuild from `frontend/admin/`. Build them from the checked-in frontend source. |
| `/.agents/`, `/.codex/config.toml`, `/.claude/launch.json` | Machine/assistant configuration, not portable application source.  |
| `build/`, `install/`, `log/`, `.venv/`, `node_modules/`, `dist/`, Python/test/typecheck caches, `*.tsbuildinfo`, coverage output | Recreate in the intended development environment. Existing nested Unity ignore files remain unchanged. |
| `unity/*/{Library,Temp,Obj,Logs,UserSettings,Build,Builds,MemoryCaptures,Recordings}/` and case variants in `.gitignore` | Editor imports/caches, user state, Linux Players, assemblies, and build evidence; regenerate rather than upload. |
| `unity/*/Assets/Observer/Visuals/` and `Visuals.meta` | Imported RB5 DAE, OMY prefab/mesh assets, welding-table OBJ, derived materials, and all associated vendor/import metadata. Ownership/redistribution evidence is incomplete. |
| `unity/*/Assets/Observer/Scenes/` and `Scenes.meta` | Generated scenes derived from the external hierarchy and visual assets. Recreate through the checked-in Editor build methods, not by opening a missing-assets scene and saving it. |
| `.env`, `.env.*`, `*.env` except documented example/template names; key/keystore extensions; SSH private-key names; `*-password.txt`, `*-token.txt`, `*.secret` | Never publish operational credentials. Re-enroll locally rather than reuse someone else's keys. An extension rule is not a content scanner. |
| SQLite `*.db`, `*.db3`, `*.sqlite`, `*.sqlite3` plus sidecars; `*.mcap`, `*.mcap.partial`, `*.bag`, `*.bag.active` | Operational state and recordings, not source fixtures. Keep failed/partial evidence locally. |
| Compiled artifacts, archives, raster/audio/video files, and PDF extensions listed in `.gitignore` | Default source-only policy, including internal planning exports. Public Markdown/SVG documentation remains eligible. Any deliberate media exception needs rights/privacy review and a narrow rule. |

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
   two URDF-importer `Library/PackageCache` sources. These pins describe the required input versions.
   Keep all 12 internal sources eligible and all pinned bytes unchanged. A clean
   clone cannot pass full source verification until the authorized external bundle
   is supplied. Do not delete manifest rows, loosen hash checks, or silently update
   pinned data to make a missing dependency pass.
5. After obtaining the exact inputs, use `extract_layout.py --check` and the
   `Sang.IsaacRb5Observer.Editor.IsaacRb5ObserverBuild.BuildLinux` /
   `Sang.IsaacScenario1Observer.Editor.IsaacScenario1ObserverBuild.BuildLinux`
   methods in the respective Unity projects under `Assets/Observer/Editor/`.
   They reconstruct scenes/visuals
   before building and audit the resulting application assemblies. Recreate local
   build evidence rather than pretending historical Player hashes describe a new
   build. Path changes, new dependency versions, or repinning need a separate review.

Reproduction instructions are not authorization to launch live services, Players,
hardware, or physical outputs. Publishing documentation does not run a Unity/Isaac build or live action.

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

## Public and Internal Documents

Plan files, assistant instructions, design reviews, audit notes and dated experiment
records belong in the private development repository. Keep public installation,
usage, requirements, security and interface documentation with the source it describes.
Do not merge the private repository wholesale into this repository; select and review
only the source and public documentation changes intended for publication.

The root `.gitignore` excludes the internal document categories from ordinary new
adds. It is not an access-control mechanism. Removing a file in a normal commit
leaves earlier versions available in Git history. Releases and tags do not change
file visibility or remove earlier versions.
