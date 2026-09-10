import type {
  SimulationValidationProfile,
  WorkflowCompatibility,
  WorkflowPublication,
} from "./operations-types";

export function simulationAdapterKey(compatibility: WorkflowCompatibility) {
  return `${compatibility.adapter_id}@${compatibility.profile_sha256}`;
}

export function latestSimulationProfile(
  publication: WorkflowPublication,
  compatibility: WorkflowCompatibility,
  profiles: SimulationValidationProfile[],
) {
  return profiles
    .filter((profile) => profile.task_definition_id === publication.task_definition_id
      && profile.task_version === publication.task_version
      && profile.adapter_id === compatibility.adapter_id
      && profile.adapter_profile_sha256 === compatibility.profile_sha256)
    .sort((left, right) => right.version - left.version)[0];
}

export function selectPublicationSimulation(
  publication: WorkflowPublication,
  profiles: SimulationValidationProfile[],
  selectedKey?: string | null,
) {
  const runnable = publication.simulation_compatibility.filter((item) => item.runnable);
  const gated = runnable.filter((item) => latestSimulationProfile(publication, item, profiles));
  // An explicit choice must not silently move to another runtime or profile digest.
  const compatibility = selectedKey !== undefined
    ? runnable.find((item) => simulationAdapterKey(item) === selectedKey)
    : runnable.length === 1 ? runnable[0] : gated.length === 1 ? gated[0] : undefined;
  const profile = compatibility ? latestSimulationProfile(publication, compatibility, profiles) : undefined;
  return { runnable, compatibility, profile };
}
