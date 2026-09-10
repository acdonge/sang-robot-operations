# Parameterized Skill Editor v1

## Decision

SANG will use a hybrid task editor:

1. **Workflow graph** for ordering, dependencies, barriers, roles, and cross-robot coordination.
2. **Versioned Skill definitions** for typed, bounded parameters such as operation, speed scale, frame references, and later artifact references.
3. **Robot-local planning** for kinematics, collision checking, trajectories, and vendor-specific execution.

Central does not become a joint-level motion programmer. A Workflow says what each role must do; the selected Agent decides how its robot performs that Skill within its commissioned envelope.

## Reference comparison

| Reference | Useful pattern | SANG decision |
| --- | --- | --- |
| ROS 2 Actions | Long-running goal with feedback, result, cancel, and preemption | Keep this execution meaning at the Agent boundary |
| MoveIt Task Constructor | Complex manipulation decomposed into typed stages and containers | Use inside a robot Agent for pick/place and planning, not as the fleet-wide UI model |
| BehaviorTree.CPP / Groot2 | Visual composition from declared node models and typed input/output ports | Reuse typed-port discovery; add conditions and fallbacks only after sequential workflows are stable |
| Tesseract Task Composer | Dataflow graph of planners, checks, and time parameterization | Keep planning pipelines local to an Agent and expose only a stable Skill contract |
| JSON Schema | Properties, required fields, closed objects, enums, and numeric bounds | Use one schema as the backend validator and UI form source |

Primary references:

- https://docs.ros.org/en/rolling/Concepts/Basic/About-Actions.html
- https://moveit.github.io/moveit_task_constructor/basics.html
- https://behaviortree.dev/docs/tutorial-basics/tutorial_11_groot2/
- https://tesseract-robotics.github.io/tesseract_nanobind/user-guide/task-composer/
- https://json-schema.org/understanding-json-schema/reference/object
- https://json-schema.org/understanding-json-schema/reference/numeric

## v1 contract

Each Skill has an immutable `capability@version` identity and a closed parameter schema. Workflow publications store only the capability reference and concrete parameter values. Changing meaning, bounds, or units requires a new capability version.

At dispatch time the orchestrator adds `bound_asset_id` as read-only system metadata. It is never shown as an editable Skill field, and the Unity executor accepts it only as a non-empty identifier; all other undeclared parameter keys remain rejected.

Scenario 1 keeps its five fixed actions and four capability identities. The operation value remains fixed by the adapter profile. The only new editable motion field is:

- `speed_scale`: dimensionless simulation speed multiplier, default `1.00`, minimum `0.50`, maximum `1.00`, step `0.05`.

The upper bound is the already tested motion. `0.50` runs at half speed and approximately doubles motion duration. The field affects only the disconnected Unity simulation. Physical command sinks remain absent.

## UI

The step editor uses a two-column inspector:

- Skill identity, purpose, category, execution mode, and safety boundary.
- Generated fields with labels, help text, units, defaults, bounds, and enum choices.

Raw JSON is retained only as a read-only contract preview for registered Skills. Unknown future/custom capabilities retain the existing advanced JSON fallback until their schema is registered.

## Future extension

- Add pose, frame, tool, object, and immutable trajectory-artifact references as new typed schema fields.
- Add reusable Skill compositions and MoveIt Task Constructor stages inside each robot Agent.
- Add Behavior Tree conditions/fallbacks after deterministic sequential execution is proven.
- Add a 3D waypoint gizmo only for simulation authoring; published paths remain immutable artifacts.
- Map Agent execution to ROS 2 Action goal/feedback/result/cancel and enforce commissioning before any physical mode is introduced.
