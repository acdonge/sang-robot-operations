# SANG Safety Shadow Contract

`shadow-v1.schema.json` is the machine-readable exchange contract for command-free Unity safety
Shadow Mode. It contains three message types:

- `SafetySnapshotV1`: Task Run-scoped, derived scene facts from the Processing PC.
- `SafetyPolicyV1`: versioned thresholds used by the evaluator.
- `SafetyAssessmentV1`: a bounded `SAFE`, `WARNING`, `UNSAFE`, or `UNKNOWN` result with reasons,
  confidence, validity, assets, and evidence references.

The JSON Schema is generated from `fleet_control_plane.safety_models`. Regenerate it after an
intentional contract change:

```bash
PYTHONPATH="$PWD/src/fleet_control_plane" \
python3 -m fleet_control_plane.safety_shadow export-contract \
  --output contracts/safety/shadow-v1.schema.json
```

Breaking field or semantic changes require a new major schema version. The v1 assessment fixes
`shadow_mode=true` and `command_emitted=false`; neither Unity nor Central may reinterpret
`task_pause_candidate` as a physical stop command.

Use `examples/default-policy.json` and `examples/safe-snapshot.json` for replay development. They
contain synthetic identity and no physical safety evidence.
