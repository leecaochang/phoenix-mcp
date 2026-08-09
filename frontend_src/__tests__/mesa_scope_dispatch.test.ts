/** Guard: each MESA scope reaches its OWN endpoints.
 *
 * The editor routes load/save/delete per inheritance level. That routing used to
 * be an if-chain ending in an untyped `return` for area, so any scope it did not
 * name was silently written to the AREA endpoint: no compile error, no runtime
 * complaint, just a profile stored against the wrong level. The injector had the
 * same shape, coercing any unrecognised scope to entity.
 *
 * Both are now exhaustive lookups, which tsc checks. What tsc cannot check is
 * that each entry calls the RIGHT endpoint, since every entry has the same
 * shape, so a copy-paste that leaves a device entry pointing at the area API
 * type-checks perfectly. These assertions read the URL that was actually
 * requested.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { deleteProfile, EDITOR_SCOPES, loadProfile, saveProfile } from "../views/MesaView";
import { parseQuickAddScope, QUICK_ADD_SCOPES } from "../inject/QuickAdd";
import { setHass } from "../api";
import type { MesaProfileScope } from "../types";

// The API path segment each scope must address. Entity profiles live under
// /mesa/profiles; every other level is named by its own plural.
const SCOPE_SEGMENT: Record<MesaProfileScope, string> = {
  entity: "profiles",
  device: "devices",
  area: "areas",
  integration: "integrations",
  domain: "domains",
};

let requests: { method: string; url: string }[];

beforeEach(() => {
  requests = [];
  setHass({
    fetchWithAuth: (path: string, init?: RequestInit) => fetch(path, init),
  });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ method: init?.method ?? "GET", url: String(url) });
    return {
      ok: true,
      status: 200,
      json: async () => ({ stored: null, warnings: [] }),
      text: async () => "{}",
    } as unknown as Response;
  }));
});

afterEach(() => {
  setHass(null);
  vi.unstubAllGlobals();
});

describe("MESA scope endpoint routing", () => {
  it("every scope reads from its own endpoint", async () => {
    for (const scope of EDITOR_SCOPES) {
      requests = [];
      await loadProfile(scope, "some-key");
      expect(requests).toHaveLength(1);
      expect(requests[0].url).toContain(`/mesa/${SCOPE_SEGMENT[scope]}/`);
    }
  });

  it("every scope writes and deletes on its own endpoint", async () => {
    for (const scope of EDITOR_SCOPES) {
      requests = [];
      await saveProfile(scope, "some-key", {});
      await deleteProfile(scope, "some-key");
      expect(requests.map((r) => r.method)).toEqual(["PUT", "DELETE"]);
      for (const req of requests) {
        expect(req.url).toContain(`/mesa/${SCOPE_SEGMENT[scope]}/`);
      }
    }
  });

  it("no two scopes share an endpoint segment", () => {
    const segments = EDITOR_SCOPES.map((s) => SCOPE_SEGMENT[s]);
    expect(new Set(segments).size).toBe(segments.length);
  });
});

describe("injector scope parsing", () => {
  it("round-trips every scope it declares", () => {
    for (const scope of QUICK_ADD_SCOPES) {
      expect(parseQuickAddScope(scope)).toBe(scope);
    }
  });

  it("falls back to entity for anything it does not recognise", () => {
    // Entity is the safe fallback: it is the most specific level, so a
    // misrouted profile affects one entity rather than a whole device or area.
    for (const raw of [null, "", "domain", "nonsense", "AREA", " device"]) {
      expect(parseQuickAddScope(raw)).toBe("entity");
    }
  });

  it("does not silently accept a scope it has no button for", () => {
    // Domain is a real scope with no Home Assistant page to inject into, so it
    // must NOT round-trip here even though it is valid elsewhere.
    expect(QUICK_ADD_SCOPES).not.toContain("domain" as never);
    expect(parseQuickAddScope("domain")).toBe("entity");
  });
});
