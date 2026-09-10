import { lazy, Suspense, useEffect, useState } from "react";
import AppShell from "./components/AppShell";
import FleetOverview from "./components/FleetOverview";
import { useFleetData } from "./fleet";
import { UiProvider } from "./theme";
import type { FleetRoute } from "./types";
import { parseRoute } from "./utils";

const RobotDetail = lazy(() => import("./components/RobotDetail"));

export default function App() {
  const fleet = useFleetData();
  const [route, setRoute] = useState<FleetRoute>(() => parseRoute());
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  useEffect(() => {
    function updateRoute() {
      setRoute(parseRoute());
    }
    window.addEventListener("hashchange", updateRoute);
    return () => window.removeEventListener("hashchange", updateRoute);
  }, []);

  useEffect(() => {
    if (!selectedAgentId || !fleet.agents.some((agent) => agent.agent_id === selectedAgentId)) {
      setSelectedAgentId(fleet.agents[0]?.agent_id || null);
    }
  }, [fleet.agents, selectedAgentId]);

  return (
    <UiProvider>
        <AppShell
          activeKey={route.view === "robot" ? "robot" : "fleet"}
          connection={fleet.connection}
          contextTitle={route.view === "robot" ? "Robot dossier" : "Fleet command"}
        >
          {route.view === "robot" ? (
            <Suspense fallback={<div className="route-loading">Loading robot dossier...</div>}>
              <RobotDetail
                agents={fleet.agents}
                events={fleet.events}
                route={route}
                summary={fleet.summary}
              />
            </Suspense>
          ) : (
            <FleetOverview
              agents={fleet.agents}
              events={fleet.events}
              onRefresh={() => void fleet.refresh()}
              onSelectAgent={setSelectedAgentId}
              refreshing={fleet.refreshing}
              selectedAgentId={selectedAgentId}
              summary={fleet.summary}
            />
          )}
        </AppShell>
    </UiProvider>
  );
}
