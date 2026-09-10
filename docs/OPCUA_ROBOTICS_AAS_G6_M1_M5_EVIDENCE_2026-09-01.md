# OPC UA Robotics, AAS, and G6 M1-M5 Evidence

- Date: 2026-09-01
- Software verdict: **PASS for M1-M5**
- Physical/site verdict: **UNVERIFIED**
- Control boundary: **No physical or process command sink**

## Delivered boundary

1. Central OPC UA now reconciles removed entities, preserves unknown quality, explicitly marks every
   application Variable read-only, and repeats its access audit after refresh.
2. Processing Node, Cell Agent, runtime Agent, Asset, and MotionDeviceSystem identities are separated
   in a versioned Robotics profile contract. SO-101, OMY, and RB5 remain honestly marked `partial`.
3. OPC UA Core, DI, IA, and Robotics 1.02 NodeSets are pinned to the Robotics 1.02 release dependency
   set and verified by full commit and SHA-256 before import. A validated reference fixture can expose
   a read-only official Robotics hierarchy in parallel with unchanged SANG v1 NodeIds.
4. Central validates the official IDTA 3.1.2 JSON metamodel, accepts JSON or a JSON-bearing AASX,
   checks complete shell-to-Submodel closure, stores immutable raw and canonical artifacts, and links
   them to an existing SANG Asset. AAS `Operation` elements are counted as metadata only and cannot be
   invoked.
5. `Sang.Observer` now defines stable Unity Asset, frame, sensor, articulation, joint, and controller
   inventory. The inventory contract requires `physical_output_connected=false` and contains no
   network or robot-command transport.
6. G6b now has append-only commissioning cases, versioned evidence packages, current/expiry checks,
   tool-payload-speed-pose stop coverage, independent safety-chain evidence, calibration, fault and
   lease timing, named roles, XR-loss behavior, resource policy, and immutable Manager/Safety Officer
   reviews bound to the exact evidence SHA-256.
7. Supporting lab files must first be uploaded to the immutable Object Store. Missing files, unknown
   artifact IDs, SHA mismatches, or damaged bytes keep the package `UNVERIFIED`.
8. AAS raw/canonical artifacts, G6b supporting files, and G6b packages are included in verified Central
   backup and restore.

## G6b safety result

The software can report `UNVERIFIED`, `READY_FOR_REVIEW`, or `REVIEWED`. `REVIEWED` means only that the
submitted lab evidence revision received its two named reviews. Every report permanently fixes:

- `command_sink_present=false`
- `physical_motion_enabled=false`
- `process_output_enabled=false`
- `can_create_physical_manifest=false`
- `g7_authorized=false`

Incomplete, expired, rejected, mismatched, or out-of-limit evidence remains `UNVERIFIED`. A review
cannot be reused after an evidence revision changes. This workflow cannot create the first physical
manifest; that remains an attended G7 activity outside this implementation.

## Verification

- Focused AAS, Unity inventory, G6b, output-boundary, and backup/restore suites pass.
- Full Fleet Control Plane regression: `329 passed` in 65.74 seconds (final rerun).
- Full OPC UA Gateway regression: `59 passed` in 12.81 seconds.
- Static source checks reject ROS publishers, vendor command SDK markers, sockets, serial transport,
  Unity network clients, digital output, and analog output in the accepted M1-M5 boundary.

## Deliberately open

- No actual user-supplied AAS file has been imported. XML-only AASX support will be added only if the
  supplied package requires it.
- No SO-101, OMY, or RB5 profile claims OPC UA Robotics conformance; manufacturer/integrator data,
  EngineeringUnits/ranges, safety structure, and CTT evidence are still required.
- The new Unity inventory source passed contract and source-boundary tests, but this record does not
  claim a fresh Unity Editor compile or attachment to the external Scenario 1 project.
- No physical stop measurement, E-stop witness record, calibrated cell evidence, or named lab approval
  has been submitted. G6b site status therefore remains `UNVERIFIED`.
- The live SO-101 OPC UA container still uses obsolete network identity configuration. Endpoint,
  certificate SAN, trust, and runtime configuration must be reprovisioned together before it can be
  represented as healthy.
