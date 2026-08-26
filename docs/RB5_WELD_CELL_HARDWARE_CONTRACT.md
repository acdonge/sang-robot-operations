# RB5 Welding Cell Hardware Contract

Status: **PROVISIONAL / NON-ENERGIZED**  
Recorded: 2026-08-21  
Applies to: Boyd 706 Robot Cell and Boyd 713 Manager Cell

This document records the hardware interpretation supplied by the operator. It is an architecture and
software contract, not an approved electrical drawing. No SANG component may energize robot motion,
wire feed, welding output, gas, or current command from this document. Final wiring and energized
commissioning require the exact equipment revisions, an approved schematic, a risk assessment, and a
qualified welding/controls engineer.

## 1. Supplied topology

The Boyd 706 Robot Cell contains the local real-time equipment and compute path:

- Rainbow Robotics RB5-850E arm and RB5 Control Box
- Miller Dynasty 400 welding power source
- CK Worldwide WF-5 cold-wire feeder and TIG torch wire guide
- TIG torch, welding cable, gas/cooler hose, and work return
- OTOS WGC400 welding camera through SDI-to-USB, plus cell cameras connected to camera/Processing PCs
- Processing PC with RTX 5080, a ZED/camera box where used, and a Galaxy XR edge operator view

The Boyd 713 Manager Cell contains the central operations path:

- Monitoring/manager laptop running the Central control plane and Admin/Operator UI
- Meta Quest manager view for monitoring and Task Pause requests
- Wireless connection to the Robot Cell Processing PC; high-rate robot and camera traffic remains local
  to the Robot Cell and only normalized state, events, selected preview, and artifacts move northbound

The RB5 Control Box is the planned local process-I/O endpoint. It exchanges robot data with the
Processing PC over LAN and is intended to interface with the WF-5, Dynasty 400, and the independent
hardwired safety chain.

## 2. Future asset identity examples

The following IDs illustrate how the equipment could be represented later. They are not seed data and
must not be created in the operational Registry until the operator decides the welding-cell asset model.
When that decision is made, the equipment should be represented as separate assets rather than as one
large robot:

| Suggested asset ID | Type | Purpose |
|---|---|---|
| `rb5_850e_01` | `robot` | Robot state and, in a later safety-reviewed phase, robot execution |
| `miller_dynasty_400_01` | `welder` | Weld output, current command, and weld-state observations |
| `ck_wf5_01` | `feeder` | Cold-wire feed state and later feed enable |
| `tig_torch_01` | `eef` | Torch identity, calibration, consumable, and maintenance metadata |
| `otos_wgc400_01` | `camera` | Welding-process video source |
| `weld_safety_chain_01` | `sensor` | Read-only projection of the independent safety-chain state |

Serial numbers, firmware, physical connector revisions, calibration, and mutable network/I/O bindings
belong to each asset's metadata or ConnectionProfile. They are not part of the stable `asset_id`.

## 3. Provisional process signal contract

Channel numbers are deliberately `TBD`. The supplied diagram describes signal intent, but does not
identify approved RB5 terminals, interposing components, connector pins, polarity, or safety category.

| Logical signal | Direction | Supplied physical intent | Safe software implementation now | Energized implementation status |
|---|---|---|---|---|
| `wire_feed_enable` | RB5 Control Box -> WF-5 | DO, wire feed on/off | Desired/observed shadow state only | Blocked pending WF-5 remote-input verification and isolated interface |
| `arc_enable` | RB5 Control Box -> Dynasty 400 | DO, arc/output on/off | Desired/observed shadow state only | Blocked pending approved Remote 14 interface and interlock review |
| `current_command_v` | RB5 Control Box -> Dynasty 400 | AO 0-10 V, remote current demand | Requested value, limits, units, and audit only; no AO write | Blocked pending range calibration and isolated analog interface review |
| `arc_valid` | Dynasty 400 -> RB5 Control Box | Arc-state feedback to DI | Read-only observed state when electrically conditioned | Blocked pending option/pin and PNP input compatibility verification |
| `safety_chain_ok` | Safety relay -> local equipment | Hardwired E-stop/interlock | Read-only status projection only | Safety action must remain independent of SANG/ROS 2/OPC UA |
| `robot_state` | RB5 Control Box -> Processing PC | Vendor data connection/LAN | Existing `rb5_readonly_adapter` joint observation | Allowed; no command-channel client |
| `weld_video` | OTOS/cameras -> Processing PC | SDI-to-USB or camera interface | Local capture, timestamp, selected preview, MCAP/object artifact | Allowed without process actuation |

Every process output must be fail-off: power-up, process restart, expired lease, stale command, network
loss, interlock loss, and Task Run termination resolve to wire feed off, weld output disabled, and current
request zero. Software fail-off behavior is defense in depth and is not a replacement for the independent
hardwired safety function.

## 4. Electrical compatibility constraints

These constraints come from current manufacturer documentation and must be rechecked against the exact
serial number and installed options before a schematic is approved.

- The RB Control Box DO00-DO15 are 24 V PNP source outputs. A single channel is rated up to 1 A and the
  total output current must remain below 2 A. They must not be treated as voltage-free contacts.
- The Dynasty 400 Remote 14 interface enables its contactor by a contact closure between its A and B
  functions. Therefore an RB5 24 V PNP DO must not be directly wired as that contact closure. The current
  architecture requires a suitably rated, isolated interposing interface that presents the welder with
  the manufacturer-required contact behavior.
- The Dynasty remote current command is a 0-10 V input referenced to its remote-control common. The RB
  Control Box AO is also 0-10 V, but Rainbow documents its analog I/O as non-isolated. Grounding,
  shielding, galvanic isolation, scaling, and fault behavior must therefore be designed and approved;
  voltage-range equality alone does not make a direct connection acceptable.
- Dynasty `valid arc` is documented as an open-collector output and is available only with the relevant
  automation option on applicable units. The RB Control Box DI is PNP+ with an 11-30 V ON range. A
  reviewed level-conversion/isolated interface is required; the supplied diagram's simple `DI` label is
  not yet a complete electrical contract.
- The WF-5 manual identifies the CWMES remote switch and an Amphenol activation receptacle, but the
  available manual does not establish a control voltage, contact rating, or connector pinout suitable
  for direct connection to the RB5 DO. Until CK supplies that information or the installed cable is
  professionally characterized, SANG treats it as an unverified switch input behind an isolated
  interposing interface.
- The safety relay must directly enforce the required safe state for the RB5, Dynasty 400, and WF-5.
  SANG may report its state but ordinary ROS 2, HTTP, Wi-Fi, or OPC UA paths are not safety-rated.

Manufacturer references:

- [Rainbow Robotics Control Box Digital Output](https://rainbowrobotics.github.io/rb_cobot_docs/manual/appendix/cbox_digital_output)
- [Rainbow Robotics Control Box I/O Overview](https://rainbowrobotics.github.io/rb_cobot_docs/manual/installation/overview_cbox_io)
- [Rainbow Robotics General Purpose Analog I/O](https://rainbowrobotics.github.io/rb_cobot_docs/manual/installation/config_general_aio)
- [Rainbow Robotics port 5001 status-data structure](https://rainbowrobotics.github.io/rb_cobot_docs/technical_docs/data_structure)
- [Miller Dynasty 400/800 Owner's Manual, OM-294202B](https://www.millerwelds.com/files/owners-manuals/O294202B_MIL.pdf)
- [Miller 14-Pin Automation Expansion Manual](https://www.millerwelds.com/-/media/miller-electric/files/system-software/tig/expansion-packs/14-pin-automation-manual_o265411f.pdf)
- [CK Worldwide WF-5 product and manual download](https://www.ckworldwide.com/products/wf-5-cold-wire-feeder)

## 5. Robot/camera-first software completion boundary

The product can be completed for robots and cameras without registering or implementing the welding
equipment. Roadmap items 1-4 and 6-8 use the following definition of completion:

1. Task Run lifecycle records configuration and transitions without dispatching equipment commands.
2. User identity, RBAC, and Edge Run Context authorize views, reservations, and dry-run operations.
3. The authenticated camera relay exposes only an explicitly selected live stream.
4. Object storage and the session catalog retain immutable MCAP/video artifacts and provenance.
5. LeRobot/VLA conversion and training remain deferred as requested.
6. Control Mode Manager defines modes, leases, TTL/deadman, approvals, and audit as a disabled/shadow
   contract for later control work; every physical command sink remains absent.
7. Typed multi-asset workflows execute validation, reservation, simulated steps, observation waits, and
   dry-run transitions for registered robot/camera assets. Physical movement and process steps are not
   dispatched.
8. Manifest-driven robot/camera adapters, Edge OPC UA projection, Central aggregation boundaries, and a
   documented adapter interface are completed. Welding-specific adapters, I/O mappings, AAS submodel
   contents, and welding asset registration remain deferred.

The current `rb5_readonly_adapter` publishes measured joints only, which is sufficient for the
robot/camera-first v1 boundary. If process-I/O observation is approved later, Rainbow's documented port
5001 status structure includes the Control Box's four analog inputs/outputs and sixteen digital
inputs/outputs, so an additive read-only observation slice is technically available without opening a
control connection. That future adapter must version-gate the decoded structure and must not construct
or import a command-channel client because the vendor states that the documented layout can change with
controller software versions.

This means the robot/camera operations product is software-complete and extensible; it does not mean
the future welding-cell integration is complete. Enabling any physical RB5 motion, WF-5 feed, Dynasty
arc, or analog current output is a separate attended milestone.
It requires an approved I/O schedule and schematic, independent interlock proof, output-disconnected
tests, load simulation, bounded live commissioning, and a recorded acceptance report.

## 6. Required confirmations before an energized design

1. Dynasty 400 serial number, exact owner's-manual revision, Remote 14 connector presence, and whether
   the Automation Expansion or Modbus Expansion is installed.
2. Proposed RB5 channel schedule: exact DO for wire feed and arc, AO for current demand, DI for arc valid
   and safety state, including boot/program-end output configuration.
3. WF-5 CWMES cable/connector documentation or an approved interface drawing that establishes pinout,
   switch behavior, voltage, and contact rating.
4. Safety relay manufacturer/model, safety architecture/category target, reset behavior, monitored
   contacts, and exactly which energy-enable circuits it interrupts.
5. Whether the current development must keep all process-I/O wires physically disconnected. The default
   and recommended answer is yes until the separate attended commissioning milestone.
