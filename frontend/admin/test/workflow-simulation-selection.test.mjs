import assert from "node:assert/strict";
import { test } from "node:test";
import {
  latestSimulationProfile,
  selectPublicationSimulation,
  simulationAdapterKey,
} from "../src/workflow-simulation-selection.ts";

const unity = { adapter_id: "unity-scenario1", profile_sha256: "u-current", runnable: true, issues: [] };
const isaac = { adapter_id: "isaac-scenario1-v1", profile_sha256: "i-current", runnable: true, issues: [] };
const publication = {
  task_definition_id: "isaac-scenario1", task_version: 1,
  simulation_compatibility: [unity, isaac],
};

function gate(adapter, updates = {}) {
  return {
    task_definition_id: publication.task_definition_id,
    task_version: publication.task_version,
    adapter_id: adapter.adapter_id,
    adapter_profile_sha256: adapter.profile_sha256,
    profile_id: `${adapter.adapter_id}-validation`, version: 1,
    ...updates,
  };
}

test("Scenario1 defaults to its current Isaac gate even when Unity is first", () => {
  const profile = gate(isaac);
  const selected = selectPublicationSimulation(publication, [profile]);
  assert.equal(selected.compatibility, isaac);
  assert.equal(selected.profile, profile);
  assert.deepEqual(selected.runnable, [unity, isaac]);
});

test("multiple runnable adapters without gates require an explicit selection", () => {
  const selected = selectPublicationSimulation(publication, []);
  assert.equal(selected.compatibility, undefined);
  assert.equal(selected.profile, undefined);
});

test("multiple current gated adapters never select the first runtime", () => {
  const profiles = [gate(unity), gate(isaac)];
  assert.equal(selectPublicationSimulation(publication, profiles).compatibility, undefined);
  assert.equal(selectPublicationSimulation({ ...publication, simulation_compatibility: [isaac, unity] }, profiles).compatibility, undefined);
});

test("stale gates cannot set a default or hide the current Isaac gate", () => {
  const oldUnity = gate(unity, { version: 20, adapter_profile_sha256: "u-old" });
  assert.equal(selectPublicationSimulation(publication, [oldUnity]).compatibility, undefined);
  assert.equal(selectPublicationSimulation(publication, [oldUnity, gate(isaac)]).compatibility, isaac);
});

test("latest gate is scoped to publication version, adapter, and current digest", () => {
  const current = gate(isaac, { version: 3 });
  const profiles = [
    gate(isaac, { version: 99, adapter_profile_sha256: "i-old" }),
    gate(isaac, { version: 98, task_version: 2 }),
    gate(isaac, { version: 97, task_definition_id: "another-task" }),
    gate(unity, { version: 96 }), gate(isaac), current,
  ];
  const before = [...profiles];
  assert.equal(latestSimulationProfile(publication, isaac, profiles), current);
  assert.deepEqual(profiles, before);
});

test("explicit Unity selection overrides the automatic Isaac gate default", () => {
  const selected = selectPublicationSimulation(publication, [gate(isaac)], simulationAdapterKey(unity));
  assert.equal(selected.compatibility, unity);
  assert.equal(selected.profile, undefined);
});

test("status and actions resolve the same explicitly selected adapter and gate", () => {
  const selectedGate = gate(isaac);
  const profiles = [gate(unity), selectedGate];
  const selectedKey = simulationAdapterKey(isaac);
  const status = selectPublicationSimulation(publication, profiles, selectedKey);
  const actions = selectPublicationSimulation(publication, profiles, selectedKey);
  assert.deepEqual(status, actions);
  assert.equal(actions.compatibility.adapter_id, selectedGate.adapter_id);
  assert.equal(actions.compatibility.profile_sha256, selectedGate.adapter_profile_sha256);
});

test("an explicit choice survives newly configured gates and reordered compatibility", () => {
  const reordered = { ...publication, simulation_compatibility: [isaac, unity] };
  assert.equal(selectPublicationSimulation(reordered, [gate(isaac), gate(unity)], simulationAdapterKey(unity)).compatibility, unity);
});

test("a removed or non-runnable explicit choice never falls back to another runtime", () => {
  const selectedKey = simulationAdapterKey(isaac);
  for (const compatibility of [[unity], [unity, { ...isaac, runnable: false }]]) {
    const selected = selectPublicationSimulation({ ...publication, simulation_compatibility: compatibility }, [gate(unity)], selectedKey);
    assert.equal(selected.compatibility, undefined);
    assert.equal(selected.profile, undefined);
  }
});

test("an explicit choice is invalidated by digest rollover, not silently rebound", () => {
  const replacement = { ...isaac, profile_sha256: "i-next" };
  const selected = selectPublicationSimulation(
    { ...publication, simulation_compatibility: [unity, replacement] },
    [gate(unity), gate(replacement)], simulationAdapterKey(isaac),
  );
  assert.equal(selected.compatibility, undefined);
  assert.equal(selected.profile, undefined);
});

test("clearing the selector leaves the choice explicit even with a unique gate", () => {
  assert.equal(selectPublicationSimulation(publication, [gate(isaac)], null).compatibility, undefined);
});

test("a single runnable adapter retains the existing configure-gate flow", () => {
  const selected = selectPublicationSimulation({ ...publication, simulation_compatibility: [isaac] }, []);
  assert.equal(selected.compatibility, isaac);
  assert.equal(selected.profile, undefined);
});

test("incompatible adapters and other publications' gates never supply a default", () => {
  const incompatible = { ...isaac, runnable: false };
  assert.deepEqual(selectPublicationSimulation({ ...publication, simulation_compatibility: [incompatible] }, [gate(isaac)]).runnable, []);
  assert.equal(selectPublicationSimulation(publication, [gate(isaac, { task_version: 2 })]).compatibility, undefined);
  assert.equal(selectPublicationSimulation(publication, [gate(isaac, { task_definition_id: "other" })]).compatibility, undefined);
});
