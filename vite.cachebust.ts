import type { Plugin } from "vite";
import { createHash } from "node:crypto";

/**
 * Appends ?v=<content-hash> to dynamic imports of emitted (non-entry) chunks.
 *
 * Chunk filenames stay stable, so the deployed frontend/ directory holds the
 * same file set every build instead of accumulating hashed variants; the query
 * param still gives every changed rebuild a URL the browser cannot have cached.
 * This is needed because the Phoenix MCP static route is registered with
 * cache_headers=False, which only omits Cache-Control (browsers then fall back
 * to heuristic caching of the bare chunk URL), and panel.py's mtime ?v=
 * cache-bust covers the ENTRY files only, never the chunks they import.
 * The hash is content-based, so an unchanged chunk keeps its URL and stays
 * validly cached.
 */
export function chunkQueryCacheBust(): Plugin {
  return {
    name: "phx-chunk-query-cache-bust",
    generateBundle(_options, bundle) {
      const hashes = new Map<string, string>();
      for (const [fileName, item] of Object.entries(bundle)) {
        if (item.type === "chunk" && !item.isEntry) {
          hashes.set(fileName, createHash("sha256").update(item.code).digest("hex").slice(0, 10));
        }
      }
      for (const item of Object.values(bundle)) {
        if (item.type !== "chunk") continue;
        for (const [fileName, hash] of hashes) {
          // All three quote styles: the emitted form is bundler-dependent and
          // has changed across major versions (Vite 5 emitted a double-quoted
          // string, Vite 8 emits a template literal). Missing one silently
          // drops the cache-bust rather than failing the build, so handle the
          // full set and assert on the output in vite_cachebust.test.ts.
          for (const q of ['"', "'", "`"]) {
            item.code = item.code.replaceAll(
              `${q}./${fileName}${q}`,
              `${q}./${fileName}?v=${hash}${q}`,
            );
          }
        }
      }
    },
  };
}
