import React from "react";
import ReactDOM from "react-dom/client";
import "antd/dist/reset.css";
import LoginApp from "./LoginApp";
import "./styles.css";
import { UiProvider } from "./theme";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <UiProvider><LoginApp /></UiProvider>
  </React.StrictMode>,
);
