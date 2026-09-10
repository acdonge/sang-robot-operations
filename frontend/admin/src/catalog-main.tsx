import React from "react";
import ReactDOM from "react-dom/client";
import "antd/dist/reset.css";
import CatalogApp from "./CatalogApp";
import "./styles.css";
import { UiProvider } from "./theme";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <UiProvider><CatalogApp /></UiProvider>
  </React.StrictMode>,
);
