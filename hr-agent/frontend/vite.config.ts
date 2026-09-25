import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the FastAPI app (agent + BFF) runs on :8000; Vite proxies /api to it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", sourcemap: false },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
