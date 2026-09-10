# Unity A1/A2 Integration

## Scope

This iteration connects the existing output-disconnected Scenario 1 Player to
stable scene asset identities. It retains Workflow Studio, immutable Task/Action
publications, the local simulation worker, and the existing action implementations.
It does not install arbitrary uploaded scripts or enable physical equipment I/O.

## Runtime Contract

- The original adapter profile and bridge protocol v1 remain unchanged.
- Adapter profile v2 embeds one exact Scene Binding Manifest, Skill/Action catalog,
  and allowlisted asset-to-Agent routes. Its digest changes when any dependency changes.
- Task role names and step names are correlation identities, not Unity object names.
  Required capabilities must resolve to exactly one installed simulation asset.
  Multiple matching robots are rejected until an explicit assignment contract exists.
- Each step must match one versioned installed Action, its fixed intent and declared
  editable inputs. No runtime file path, C# type name, vendor endpoint or joint is
  accepted from the browser.
- The worker checks live inventory, stable bindings, package digests, frame/sensor
  presence, articulation/controller identities, and exact installed Action source hashes.
  Diagnostic hierarchy names are not execution selectors.
- Bridge command/result v2 pins the scene digest, asset binding, controller binding,
  versioned Action key and source hash. A result from a different binding cannot
  complete the step. Idempotent retries must reproduce the exact command content.
- Actual motion still belongs to the existing installed Unity action/controller.
  Changing the workflow order does not bypass its phase/precondition checks.
- Current dynamic execution supports one exclusive asset per role and a linear
  sequence of Actions, without retries, fan-out, compensation, or zone selectors.
  Unsupported or ambiguous requests fail closed rather than silently choosing.

## Packaging and Deployment

`scripts/build_unity_dynamic_profile.py` generates a versioned adapter from trusted
local scene/catalog/config inputs. It refuses to overwrite a different generated
profile. The existing v1 profile is not mutated.

`SANG_UNITY_ADAPTER_PROFILE` selects the checked-in profile for Admin, Manager and
the simulation worker. Unset keeps the v1 default. All processes must use the same
digest. A changed profile requires fresh validation evidence; prior Run results
and publications are not rewritten.

The deployed profile is `contracts/tasks/iitp-scenario1-unity-adapter.v3.json`
(bridge/profile schema 2; filename revision 3). The v2 file is the earlier build
candidate, not the final reset-order fix. Final profile SHA-256:
`7c779898cd75a711ff2d61e45964eb9b70a04dc1551493290e9105618ee3a75b`.
Final scene manifest SHA-256:
`4fa52c13e174ba2f7072385575dcd3feeaa862042f27d9826436a7ede9fec05b`.
The Supervisor runs the locally built `Builds/A1PlayerFinal/` executable in the
actual Scenario1_app project. Admin, Manager and simulation Worker use the same
profile. Existing observation/recording services were not restarted.

The source-backed scene archive is not a signed release qualification and is not
automatically installed from an Action Library upload. Trusted build/signing,
reusable packages in a second scene, and ROS 2/MCAP observation parity are separate
follow-up work. The scene uses the Unity simulation clock; this iteration does not
prove physical/simulation clock alignment.

## Verification

- Initial focused backend tests: 63 passed.
- Final full Fleet regression after the rollover fix: 360 passed (66.70 s), one
  existing Starlette warning. The last-added blocked-worker test was verified in
  the separate 15-case focused run; it was added after full-suite collection.
- Actual signed dynamic-dispatch, lease-loss and profile-rollover regressions: 15 passed.
- Final Unity Editor build: passed (6000.5.6f1); 12 Editor binding rejection tests
  passed. Runtime reports nine valid, uniquely bound assets with no inventory errors.
- Real negative bridge requests (12): rejected without motion; final smoke accepted
  a renamed carrier step with clean Action inputs.
- Private final-Player regression: fresh legacy v1 19 checks PASS; fresh schema-2
  run with arbitrary step IDs and clean parameters 19 checks PASS. Both completed
  all five Actions and Reset. Three inventory-only Reset cycles: 16 checks PASS.
  Exact aggregate: actual project's `Builds/A1Evidence/final-acceptance.json`.
- Actual UI Run: COMPLETE, `sim-95bf4a8aef1a433e8be1e865f0797da5`, copied and
  published Task `unity-assets-a2-20260909@1`, validation Profile
  `unity-assets-a2-20260909-unity-validation@1`.
- Run duration: 162.034 s. Carrier delivery, OMY placement, RB5 path, OMY return and
  carrier return all SUCCEEDED. The web-requested Reset was ACKNOWLEDGED at
  2026-09-09 14:26:57 UTC; all six validation checks PASS. The post-reset Player
  reports READY/ReadyDelivery, nine valid bindings, the same scene digest and
  `physical_output_connected=false`.
- Raw dated snapshots: `runtime/phase3-lab1/unity-scenario1/a1-a2-evidence/web-run.json`
  and `runtime-after-reset.json`. Unity build/reference evidence is in
  `A1_UNITY_INVENTORY_2026-09-09.md` and the actual project's `Builds/A1Evidence/`.
- Claude Opus review: not executed; CLI OAuth expired and refresh failed.
- Independent Codex review: scoped A1/A2 PASS, both P1s and final P2 closed; no
  remaining P1/P2 findings in the reviewed slice. Reviewer ran the 15-case suite
  and an affected 67-test combined suite. This is not independent certification
  of physical isolation or production readiness.

## Review Corrections and Limits

- The real orchestrator injects `bound_asset_id`. It is checked against both the
  signed goal and resolved asset, then removed from Action inputs before comparison
  and Unity dispatch. Missing or contradictory metadata fails closed.
- Worker lease conflicts now latch abort. Each command/recovery write checks the
  current DB owner under a short transaction and the last confirmed monotonic lease
  deadline, preventing new dispatch after ownership loss or an expired renewal.
- The claim transaction also compares the Run's pinned profile/scenario to the
  deployed profile before either queued execution or restart reconciliation. A
  rollover mismatch blocks claiming and advertises the reason; restoring the
  exact prior deployment allows recovery. It never rewrites the Run to the new
  profile or silently redirects the same role to a different robot.
- Inventory bootstrap waits until the existing carrier has finished replacing its
  scene object. Reset inventory now rebinds all nine objects rather than retaining
  the destroyed carrier/workpiece references. URDF joints are selected by actual
  UrdfLink/ArticulationBody identity, not duplicate visual mesh names.
- Separate repeated-motion regression exposed the existing OMY IK guard after
  Reset (reported target error 0.209 m versus 0.20 m limit). No threshold, trajectory,
  collision or physical safety guard was loosened. Reset inventory validity does
  not establish repeatable motion; repeat-run qualification remains open. Root
  cause is not isolated to baseline behavior versus runtime timing.
- Binding IDs are a runtime contract, not an automatic import into Central Asset
  Registry. The seven source-backed package documents/archive remain local build
  evidence, not a signed or Object-Store-registered reusable release.
- After the final Admin restart the already-open page retained a fetch-error
  banner until reload. Reload restored Central live, READY and the persisted PASS
  result. Automatic frontend reconnect was not changed in this backend/Unity slice.

## Separate Deployment Debt

The SO-101 read-only gateway remains unavailable: old host address and expired
revocation lists require coordinated network/PKI migration. No security checks
were bypassed. See `SO101_OPCUA_IDENTITY_2026-09-09.md` for measured details.

No physical commissioning, welding/feeder output, certified safety judgment,
OPC UA conformance, or completed AAS user content is claimed by these results.
