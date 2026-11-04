import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  // PWA shell (manifest, service worker, offline page, icons) is copied
  // verbatim into dist/client by Vite. Without this the profile builds an
  // app whose manifest/sw 404 at runtime (gate review IN-03).
  publicDir: "source/client/public",
  build: {
    outDir: "dist/client",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:3000",
      "/healthz": "http://localhost:3000",
    },
  },
});
