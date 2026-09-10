import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    assetsDir: "antd-assets",
    emptyOutDir: false,
    outDir: "../../ui-admin",
    rollupOptions: {
      input: {
        actions: fileURLToPath(new URL("./actions.html", import.meta.url)),
        admin: fileURLToPath(new URL("./index.html", import.meta.url)),
        catalog: fileURLToPath(new URL("./catalog.html", import.meta.url)),
        config: fileURLToPath(new URL("./config.html", import.meta.url)),
        login: fileURLToPath(new URL("./login.html", import.meta.url)),
        operator: fileURLToPath(new URL("./operator.html", import.meta.url)),
        workflow: fileURLToPath(new URL("./workflow.html", import.meta.url)),
      },
      output: {
        assetFileNames: "antd-assets/[name].[ext]",
        chunkFileNames: "antd-assets/[name].js",
        entryFileNames: (chunk) => chunk.name === "workflow"
          ? "antd-assets/workflow-[hash].js"
          : "antd-assets/[name].js",
      },
    },
    sourcemap: false,
  },
});
