import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API runs separately (uvicorn on :8000). Proxying keeps the browser on a
// single origin in development, so the UI never hits CORS during local work.
const apiTarget = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/images": apiTarget,
      "/jobs": apiTarget,
      "/health": apiTarget,
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
