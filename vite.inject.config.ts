import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { chunkQueryCacheBust } from "./vite.cachebust";

// Second build target: the in-context profile injector. Emitted alongside the
// panel bundle into custom_components/phoenix_mcp/frontend/ (emptyOutDir: false so it does
// not wipe phoenix-mcp-panel.js). ES-module format (loaded via the frontend extra-module
// mechanism) with code-splitting left ON, so the heavy modal (React +
// ProfileEditor) becomes a separate chunk that the tiny always-on injector
// lazy-imports only on first use.
export default defineConfig({
  plugins: [react(), chunkQueryCacheBust()],
  base: "/local/phoenix-mcp/",
  build: {
    outDir: "custom_components/phoenix_mcp/frontend",
    emptyOutDir: false,
    rollupOptions: {
      input: resolve(__dirname, "frontend_src/inject/index.ts"),
      output: {
        format: "es",
        entryFileNames: "phoenix-mcp-inject.js",
        // Stable chunk name; freshness comes from chunkQueryCacheBust, which
        // rewrites the entry's lazy import to ./phoenix-mcp-inject-quickadd.js?v=<hash>
        // (see vite.cachebust.ts for why a bare chunk URL goes stale).
        chunkFileNames: "phoenix-mcp-inject-quickadd.js",
        assetFileNames: "[name][extname]",
      },
    },
    target: "es2020",
    minify: true,
  },
});
