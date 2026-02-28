import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Load VITE_* vars from project-root .env (single shared env file).
  envDir: "..",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
