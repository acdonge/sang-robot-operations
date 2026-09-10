import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import WorkflowStudioApp from "./WorkflowStudioApp";
import { UiProvider } from "./theme";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <UiProvider><WorkflowStudioApp /></UiProvider>
  </StrictMode>,
);
