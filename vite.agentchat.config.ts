import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { chunkQueryCacheBust } from "./vite.cachebust";

// Third build target: the global Agent Chat window module. Loaded on every HA
// page via the frontend extra-module mechanism (like the profile injector) so the
// chat window can float over the whole HA UI. Emitted alongside the panel and
// injector bundles into custom_components/phoenix_mcp/frontend/ (emptyOutDir: false).
// Code-splitting stays ON so the heavy window (React + AgentCliWindow + panel CSS)
// is a separate chunk the tiny always-on bootstrap lazy-imports on first open.
export default defineConfig({
  plugins: [react(), chunkQueryCacheBust()],
  base: "/local/phoenix-mcp/",
  build: {
    outDir: "custom_components/phoenix_mcp/frontend",
    emptyOutDir: false,
    rollupOptions: {
      input: resolve(__dirname, "frontend_src/inject/agentchat.ts"),
      output: {
        format: "es",
        entryFileNames: "phoenix-mcp-agentchat.js",
        // Stable chunk name; freshness comes from chunkQueryCacheBust, which
        // rewrites the entry's lazy import to ./phoenix-mcp-agentchat-window.js?v=<hash>
        // (see vite.cachebust.ts for why a bare chunk URL goes stale).
        chunkFileNames: "phoenix-mcp-agentchat-window.js",
        assetFileNames: "[name][extname]",
      },
    },
    target: "es2020",
    minify: true,
  },
});
