import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile, rename, copyFile } from 'node:fs/promises';
import path from 'node:path';

// This harness starts only the explicitly supplied disconnected Player and uses
// a unique local IPC directory, never the operator's bridge or Editor process.
const [player, evidence, mode = 'both'] = process.argv.slice(2);
assert(player && evidence && ['both', '1', '2', 'reset'].includes(mode),
  'Usage: node runtime_bridge_test.mjs <isolated-player> <new-evidence-directory> [both|1|2|reset]');
await mkdir(evidence, { recursive: true });
const bridge = path.join(evidence, 'bridge');
const env = { ...process.env, SANG_UNITY_ORCHESTRATOR: '1', SANG_UNITY_BRIDGE_ROOT: bridge };
delete env.IITP_RB5_SIM_TO_REAL;
delete env.IITP_OMY_SIM_TO_REAL;
const child = spawn(player, ['-batchmode', '-nographics', '-logFile', path.join(evidence, 'Player.log')], { env, stdio: 'ignore' });
const checks = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function json(file) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
async function waitFor(name, fn, seconds = 60) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    assert(child.exitCode === null, `Player exited unexpectedly: ${child.exitCode}`);
    const value = await fn();
    if (value) return value;
    await pause(200);
  }
  throw new Error(`Timed out: ${name}; heartbeat=${JSON.stringify(await heartbeat())}`);
}
const heartbeat = () => json(path.join(bridge, 'heartbeat.json'));
async function put(directory, id, value) {
  const file = path.join(bridge, directory, `${id}.json`);
  await writeFile(`${file}.tmp`, JSON.stringify(value));
  await rename(`${file}.tmp`, file);
}
let serial = 0;
function command(route, snapshot, version, runId, changes = {}) {
  const id = `a1-cmd-${++serial}`;
  const asset = snapshot.scene_inventory.bindings.find(binding => binding.unity_binding_id === route.unity_binding_id);
  return {
    schema_version: version, scenario_id: snapshot.scenario_id, command_id: id, run_id: runId,
    step_id: version === 2 ? `arbitrary-correlation-${serial}` : route.legacy_step_id,
    agent_id: `a1-test-${asset.identity.asset_id}`, capability: route.capability,
    parameters: { operation: route.operation, speed_scale: 1.0,
      ...(version === 1 ? { bound_asset_id: asset.identity.asset_id } : {}) },
    issued_at: new Date().toISOString(), not_valid_after: new Date(Date.now() + 900000).toISOString(),
    execution_mode: 'SIMULATION', physical_output_connected: false,
    ...(version === 2 ? {
      unity_binding_id: route.unity_binding_id, controller_component_binding_id: route.controller_component_binding_id,
      action_key: route.action_key, scene_manifest_sha256: snapshot.scene_manifest_sha256, action_source_sha256: route.source_sha256,
    } : {}), ...changes,
  };
}
async function send(value, expected, label) {
  await put('commands', value.command_id, value);
  const result = await waitFor(label, () => json(path.join(bridge, 'results', `${value.command_id}.json`)), 300);
  assert.equal(result.state, expected, `${label}: ${result.detail}`);
  assert.equal(result.step_id, value.step_id);
  assert.equal(result.run_id, value.run_id);
  assert.equal(result.physical_output_connected, false);
  assert.equal(result.output_isolation_confirmed, true);
  if (value.schema_version === 2) {
    assert.equal(result.schema_version, 2);
    for (const key of ['unity_binding_id', 'controller_component_binding_id', 'action_key', 'scene_manifest_sha256', 'action_source_sha256'])
      assert.equal(result[key], value[key], `${label}: result ${key} echo`);
  }
  checks.push({ name: label, result });
  console.log(`${expected}: ${label}`);
  return result;
}
async function reset(runId) {
  const id = `a1-reset-${++serial}`;
  const value = { schema_version: 1, scenario_id: 'iitp-scenario1', control_id: id, run_id: runId, action: 'RESET',
    execution_mode: 'SIMULATION', physical_output_connected: false,
    issued_at: new Date().toISOString(), not_valid_after: new Date(Date.now() + 60000).toISOString() };
  await put('controls', id, value);
  const result = await waitFor('reset acknowledged', () => json(path.join(bridge, 'control-results', `${id}.json`)));
  assert.equal(result.state, 'ACKNOWLEDGED', result.detail);
  const ready = await waitFor('fresh inventory after scene reset', async () => {
    const state = await heartbeat();
    return state?.ready && state.inventory_valid && state.observed_phase === 'ReadyDelivery' && !state.current_run_id
      && Date.parse(state.updated_at) >= Date.parse(result.completed_at) && state;
  });
  checks.push({ name: 'reset rebinds all nine assets', result });
  return ready;
}

try {
  const initial = await waitFor('ready inventory', async () => {
    const state = await heartbeat();
    return state?.ready && state.inventory_valid && state;
  }, 90);
  assert.equal(initial.scene_inventory.bindings.length, 9);
  assert.equal(initial.action_bindings.length, 5);
  assert.equal(initial.scene_inventory.unity_version, '6000.5.6f1');
  assert.equal(initial.physical_output_connected, false);
  await writeFile(path.join(evidence, 'initial-heartbeat.json'), JSON.stringify(initial, null, 2));
  await pause(5000);
  const idle = await heartbeat();
  assert.equal(idle.observed_phase, 'ReadyDelivery');
  assert.equal(idle.active_command_id, null);
  assert.equal(idle.current_run_id, null);
  assert.deepEqual(idle.scene_inventory, initial.scene_inventory);
  checks.push({ name: 'five-second stable inventory and no automatic action', passed: true });
  if (mode === 'reset') {
    for (let iteration = 0; iteration < 3; iteration++) {
      const resetSmoke = await reset('a1-reset-smoke');
      assert.deepEqual(resetSmoke.scene_inventory, initial.scene_inventory);
      console.log('PASSED: reset smoke rebinds all nine assets without automatic action');
    }
  }
  const routes = initial.action_bindings;
  for (const [field, bad] of [
    ['scene_manifest_sha256', '0'.repeat(64)], ['action_source_sha256', '0'.repeat(64)],
    ['unity_binding_id', routes[2].unity_binding_id], ['controller_component_binding_id', routes[2].controller_component_binding_id],
    ['action_key', 'unregistered-action@1'], ['capability', 'other-capability@1'], ['physical_output_connected', true],
    ['execution_mode', 'PHYSICAL'], ['not_valid_after', '2000-01-01T00:00:00Z'],
  ]) await send(command(routes[0], initial, 2, 'a1-invalid', { [field]: bad }), 'REJECTED', `reject ${field}`);
  await send(command(routes[0], initial, 2, 'a1-invalid', {
    parameters: { operation: routes[0].operation, bound_asset_id: 'wrong-asset' },
  }), 'REJECTED', 'reject cross-asset parameter');
  await send(command(routes[2], initial, 2, 'a1-invalid'), 'REJECTED', 'reject unsafe phase reorder');
  await send(command(routes[0], initial, 1, 'a1-invalid', { action_key: routes[0].action_key }), 'REJECTED', 'reject schema downgrade');
  let snapshot = initial;
  for (const version of mode === 'reset' ? [] : mode === 'both' ? [2, 1] : [Number(mode)]) {
    const runId = `a1-runtime-schema-${version}`;
    for (const route of routes) {
      await waitFor('next action ready', async () => (await heartbeat())?.ready);
      await send(command(route, snapshot, version, runId), 'SUCCEEDED', `schema ${version} ${route.action_key}`);
    }
    await waitFor('complete phase', async () => (await heartbeat())?.observed_phase === 'Complete');
    snapshot = await reset(runId);
    assert.equal(snapshot.scene_manifest_sha256, initial.scene_manifest_sha256);
    assert.deepEqual(snapshot.scene_inventory, initial.scene_inventory);
  }
  await copyFile(path.join(bridge, 'heartbeat.json'), path.join(evidence, 'final-heartbeat.json'));
  await writeFile(path.join(evidence, 'runtime-tests.json'), JSON.stringify({ passed: true, checks }, null, 2));
  console.log(`PASS: ${checks.length} runtime checks`);
} catch (error) {
  await writeFile(path.join(evidence, 'runtime-tests.json'), JSON.stringify({ passed: false, error: String(error), checks }, null, 2));
  console.error(error);
  process.exitCode = 1;
} finally {
  child.kill('SIGTERM');
  await pause(1000);
}
