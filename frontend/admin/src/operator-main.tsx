import React from "react";
import ReactDOM from "react-dom/client";
import "antd/dist/reset.css";
import OperatorApp from "./OperatorApp";
import "./styles.css";
import { UiProvider } from "./theme";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <UiProvider><OperatorApp /></UiProvider>
  </React.StrictMode>,
);
