import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built files are served by `python -m forgeflow.workbench` at /workbench/.
// In `npm run dev`, API calls go to that same local server.
const api = "http://127.0.0.1:8780";

export default defineConfig({
  base: "/workbench/",
  plugins: [react()],
  server: {
    proxy: {
      "/fabric": api,
      "/runs": api,
      "/health": api,
      "/workbench-config": api,
    },
  },
  test: { environment: "node" },
});
