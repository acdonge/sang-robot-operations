import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import ActionLibraryApp from "./ActionLibraryApp";
import { UiProvider } from "./theme";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <UiProvider><ActionLibraryApp /></UiProvider>
  </StrictMode>,
);
