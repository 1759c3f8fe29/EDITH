import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Port 1420 is Tauri's convention, and the core's CORS list already allows it
// along with 5173. Fixed rather than auto-incrementing: if the port moves, the
// core silently starts rejecting requests as cross-origin, and the UI would show
// "cannot reach the core" for what is actually a CORS refusal.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 1420,
    strictPort: true,
    // The core is a separate process on 8787. Proxied rather than called
    // cross-origin so that dev and the packaged Tauri build (where the UI is on
    // tauri://localhost) exercise the same relative URLs.
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
  build: {
    // Relative, not absolute: an absolute base produces a dist that loads
    // nothing under file://, which is how the packaged shell serves it.
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  base: "./",
});
