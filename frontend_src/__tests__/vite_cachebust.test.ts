/** The lazy-chunk cache-bust must survive a bundler upgrade.
 *
 * chunkQueryCacheBust() rewrites dynamic imports of emitted chunks to carry
 * ?v=<content-hash>. The panel's static route is registered with
 * cache_headers=False, so browsers fall back to heuristic caching of the bare
 * chunk URL; panel.py's own mtime cache-bust covers ENTRY files only, never
 * the chunks they import.
 *
 * The rewrite is a string replacement over emitted code, so it depends on how
 * the bundler quotes the import specifier -- and that changed across a major
 * version (Vite 5 emitted "./chunk.js", Vite 8 emits `./chunk.js`). It fails
 * OPEN: a missed quote style drops the cache-bust with a successful build and
 * no warning, which is exactly how it regressed once. These assert each style.
 */
import { describe, it, expect } from "vitest";
import { chunkQueryCacheBust } from "../../vite.cachebust";

type Chunk = { type: "chunk"; isEntry: boolean; code: string };

function runPlugin(bundle: Record<string, Chunk>) {
  const plugin = chunkQueryCacheBust();
  // generateBundle is declared as a plain function in the plugin object.
  (plugin.generateBundle as unknown as (o: unknown, b: unknown) => void).call(
    plugin,
    {},
    bundle,
  );
  return bundle;
}

function bundleWith(importExpr: string) {
  return {
    "entry.js": { type: "chunk" as const, isEntry: true, code: `import(${importExpr})` },
    "lazy.js": { type: "chunk" as const, isEntry: false, code: "export const a = 1;" },
  };
}

describe("chunkQueryCacheBust", () => {
  it.each([
    ['double quotes (Vite 5)', '"./lazy.js"'],
    ['single quotes', "'./lazy.js'"],
    ['template literal (Vite 8)', "`./lazy.js`"],
  ])("appends ?v=<hash> for %s", (_label, expr) => {
    const out = runPlugin(bundleWith(expr));
    expect(out["entry.js"].code).toMatch(/\.\/lazy\.js\?v=[a-f0-9]{10}/);
  });

  it("keeps the quote style it found", () => {
    const out = runPlugin(bundleWith("`./lazy.js`"));
    expect(out["entry.js"].code).toMatch(/`\.\/lazy\.js\?v=[a-f0-9]{10}`/);
  });

  it("is content-addressed, so an unchanged chunk keeps its URL", () => {
    const first = runPlugin(bundleWith('"./lazy.js"'))["entry.js"].code;
    const second = runPlugin(bundleWith('"./lazy.js"'))["entry.js"].code;
    expect(first).toBe(second);
  });

  it("gives a changed chunk a different URL", () => {
    const a = runPlugin(bundleWith('"./lazy.js"'))["entry.js"].code;
    const changed = bundleWith('"./lazy.js"');
    changed["lazy.js"].code = "export const a = 2;";
    const b = runPlugin(changed)["entry.js"].code;
    expect(a).not.toBe(b);
  });

  it("leaves entry chunks unhashed (panel.py cache-busts those by mtime)", () => {
    const bundle = {
      "entry.js": { type: "chunk" as const, isEntry: true, code: 'import("./entry.js")' },
    };
    const out = runPlugin(bundle);
    expect(out["entry.js"].code).not.toContain("?v=");
  });
});
