import { describe, it, expect, vi, beforeEach } from "vitest";
import { api, setHass, ApiError, localizedApiMessage } from "../api";
import { primeTranslations } from "../i18n";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";

function mockFetchResponse(status: number, body: unknown = {}) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: () => Promise.resolve(body),
  });
}

beforeEach(() => {
  setHass({
    fetchWithAuth: (path: string, init?: RequestInit) => fetch(path, init),
  });
});

describe("api.listTokens", () => {
  it("sends GET to /api/phoenix-mcp/admin/tokens", async () => {
    const tokens = [{ id: "1", name: "test" }];
    globalThis.fetch = mockFetchResponse(200, tokens);

    const result = await api.listTokens();
    expect(result).toEqual(tokens);
    expect(fetch).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/tokens",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("api.createToken", () => {
  it("sends POST with JSON body", async () => {
    const created = { id: "1", name: "new", token: "phx_abc" };
    globalThis.fetch = mockFetchResponse(201, created);

    const result = await api.createToken({ name: "new" });
    expect(result).toEqual(created);

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/tokens");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ name: "new" });
  });
});

describe("api.patchToken", () => {
  it("sends PATCH with correct URL and body", async () => {
    const updated = { id: "tok-1", name: "test", pass_through: true };
    globalThis.fetch = mockFetchResponse(200, updated);

    await api.patchToken("tok-1", { pass_through: true, confirm_pass_through: true });

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/tokens/tok-1");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body)).toEqual({ pass_through: true, confirm_pass_through: true });
  });
});

describe("api.revokeToken", () => {
  it("sends DELETE to the correct URL", async () => {
    globalThis.fetch = mockFetchResponse(204);

    await api.revokeToken("tok-1");
    expect(fetch).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/tokens/tok-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});

describe("api.wipe", () => {
  it("sends DELETE with confirm body", async () => {
    globalThis.fetch = mockFetchResponse(204);

    await api.wipe();

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/wipe");
    expect(opts.method).toBe("DELETE");
    expect(JSON.parse(opts.body)).toEqual({ confirm: "WIPE" });
  });

  it("passes the selected scope flags alongside confirm", async () => {
    globalThis.fetch = mockFetchResponse(204);

    await api.wipe({ wipe_core: true, wipe_providers: false, wipe_mesa: true });

    const [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({
      confirm: "WIPE",
      wipe_core: true,
      wipe_providers: false,
      wipe_mesa: true,
    });
  });
});

describe("api.getAudit", () => {
  it("builds query string from params", async () => {
    globalThis.fetch = mockFetchResponse(200, []);

    await api.getAudit({ limit: 50, outcome: "denied", token_id: "tok-1" });
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("limit=50");
    expect(url).toContain("outcome=denied");
    expect(url).toContain("token_id=tok-1");
  });

  it("sends no query string when params are empty", async () => {
    globalThis.fetch = mockFetchResponse(200, []);

    await api.getAudit();
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/audit");
  });
});

describe("api.resolve", () => {
  it("encodes entity_id in the URL", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_id: "light.kitchen", effective: "WRITE" });

    await api.resolve("tok-1", "light.kitchen");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/tokens/tok-1/resolve/light.kitchen");
  });
});

describe("api.getTokenConnection", () => {
  it("GETs the connection endpoint for the token", async () => {
    globalThis.fetch = mockFetchResponse(200, { last_used_at: null, request_count: 0 });
    await api.getTokenConnection("tok-1");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/tokens/tok-1/connection");
    expect(opts.method).toBe("GET");
  });
});

describe("api entity hints", () => {
  it("getEntityHints GETs the global hints map", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_hints: {} });
    await api.getEntityHints();
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/entity-hints");
    expect(opts.method).toBe("GET");
  });

  it("setEntityHint PUTs the hint for an encoded entity id", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_hints: { "light.a": "note" } });
    await api.setEntityHint("light.a", "note");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/entity-hints/light.a");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual({ hint: "note" });
  });
});

describe("api MESA profile endpoints", () => {
  it("listMesaProfiles builds a filtered query", async () => {
    globalThis.fetch = mockFetchResponse(200, { profiles: [], total_matched: 0, has_more: false, next_cursor: null });
    await api.listMesaProfiles({ domain: "light", limit: 50 });
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/api/phoenix-mcp/admin/mesa/profiles?");
    expect(url).toContain("domain=light");
    expect(url).toContain("limit=50");
  });

  it("getMesaProfile encodes the entity_id", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_id: "light.a", stored: null, effective: {}, explanation: {} });
    await api.getMesaProfile("light.a");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/profiles/light.a");
  });

  it("putMesaProfile sends PUT with the document body", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_id: "light.a", stored: {}, warnings: [] });
    const doc = { semantic_profile: { semantic_tags: ["lighting.ambient"] } };
    await api.putMesaProfile("light.a", doc);
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/profiles/light.a");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual(doc);
  });

  it("deleteMesaProfile sends DELETE", async () => {
    globalThis.fetch = mockFetchResponse(200, { entity_id: "light.a", deleted: true });
    await api.deleteMesaProfile("light.a");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/profiles/light.a");
    expect(opts.method).toBe("DELETE");
  });

  it("getMesaIssues adds refresh flag when requested", async () => {
    globalThis.fetch = mockFetchResponse(200, { issues: [], orphans: [] });
    await api.getMesaIssues(true);
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/issues?refresh=1");
  });

  it("getMesaIssues scopes to suggestions-only when requested", async () => {
    globalThis.fetch = mockFetchResponse(200, { issues: [], orphans: [] });
    await api.getMesaIssues("suggestions");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/issues?refresh=suggestions");
  });

  it("putMesaDomain sends PUT to the domain endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { domain: "light", stored: {} });
    const doc = { semantic_profile: { operational_boundaries: { control_mode: "confirm" } } };
    await api.putMesaDomain("light", doc);
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/domains/light");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual(doc);
  });

  it("deleteMesaDomain sends DELETE to the domain endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { domain: "light", deleted: true });
    await api.deleteMesaDomain("light");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/domains/light");
    expect(opts.method).toBe("DELETE");
  });

  it("listMesaDomains sends GET to the domains endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { domains: [{ domain: "lock", document: {} }] });
    const result = await api.listMesaDomains();
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/domains");
    expect(opts.method).toBe("GET");
    expect(result.domains[0].domain).toBe("lock");
  });

  it("listMesaAreas sends GET to the areas endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { areas: [{ area_id: "bedroom", document: {} }] });
    const result = await api.listMesaAreas();
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/areas");
    expect(opts.method).toBe("GET");
    expect(result.areas[0].area_id).toBe("bedroom");
  });

  it("getMesaVocabulary sends GET to the vocabulary endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { canonical_tags: ["lighting.ambient"], canonical_roots: ["lighting"] });
    const result = await api.getMesaVocabulary();
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/vocabulary");
    expect(opts.method).toBe("GET");
    expect(result.canonical_tags).toContain("lighting.ambient");
  });

  it("putMesaArea and deleteMesaArea hit the area endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { area_id: "bedroom", stored: {} });
    await api.putMesaArea("bedroom", { semantic_profile: {} });
    let [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/areas/bedroom");
    expect(opts.method).toBe("PUT");

    globalThis.fetch = mockFetchResponse(200, { area_id: "bedroom", deleted: true });
    await api.deleteMesaArea("bedroom");
    [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/areas/bedroom");
    expect(opts.method).toBe("DELETE");
  });
});

describe("api.patchSettings", () => {
  it("sends PATCH with partial settings", async () => {
    const updated = { kill_switch: true };
    globalThis.fetch = mockFetchResponse(200, updated);

    await api.patchSettings({ kill_switch: true });

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/settings");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body)).toEqual({ kill_switch: true });
  });

  it("binds and unbinds the Assist token", async () => {
    globalThis.fetch = mockFetchResponse(200, { assist_bound_token_id: "tok-1" });
    await api.patchSettings({ assist_bound_token_id: "tok-1" });
    let [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ assist_bound_token_id: "tok-1" });

    globalThis.fetch = mockFetchResponse(200, { assist_bound_token_id: null });
    await api.patchSettings({ assist_bound_token_id: null });
    [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ assist_bound_token_id: null });
  });

  it("patches voice-agent config fields", async () => {
    globalThis.fetch = mockFetchResponse(200, {});
    await api.patchSettings({ voice_agent_enabled: true, voice_agent_provider_id: "i1" });
    const [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ voice_agent_enabled: true, voice_agent_provider_id: "i1" });
  });

  it("patches AI Task config fields", async () => {
    globalThis.fetch = mockFetchResponse(200, {});
    await api.patchSettings({ ai_task_enabled: true, ai_task_provider_id: "i1" });
    const [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ ai_task_enabled: true, ai_task_provider_id: "i1" });
  });
});

describe("api voice-agent Assist pipeline", () => {
  it("POSTs the preferred flag to create the pipeline", async () => {
    globalThis.fetch = mockFetchResponse(200, { pipeline_id: "pl1", name: "Phoenix MCP", preferred: true });
    const r = await api.createVoiceAgentPipeline(true);
    expect(r.pipeline_id).toBe("pl1");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/voice_agent/pipeline");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ preferred: true });
  });

  it("DELETEs the pipeline endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { ok: true });
    await api.deleteVoiceAgentPipeline();
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/voice_agent/pipeline");
    expect(opts.method).toBe("DELETE");
  });
});

describe("api AI Task preferred entity", () => {
  it("GET/POST/DELETE hit the ai_task/preferred endpoint", async () => {
    globalThis.fetch = mockFetchResponse(200, { supported: true, is_preferred: false });
    await api.getAiTaskPreferred();
    let [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/ai_task/preferred");
    expect(opts.method).toBe("GET");

    globalThis.fetch = mockFetchResponse(200, { supported: true, is_preferred: true });
    await api.setAiTaskPreferred();
    [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/ai_task/preferred");
    expect(opts.method).toBe("POST");

    globalThis.fetch = mockFetchResponse(200, { supported: true, is_preferred: false });
    await api.clearAiTaskPreferred();
    [, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(opts.method).toBe("DELETE");
  });
});

describe("error handling", () => {
  it("throws ApiError on non-ok responses", async () => {
    globalThis.fetch = mockFetchResponse(403, { error: "forbidden", message: "Access denied." });

    await expect(api.listTokens()).rejects.toThrow(ApiError);
    try {
      await api.listTokens();
    } catch (e) {
      expect((e as ApiError).status).toBe(403);
      expect((e as ApiError).code).toBe("forbidden");
    }
  });

  it("localizes a backend error when it supplies a message key", async () => {
    primeTranslations({ adminError: { providerAlreadyConfigured: "此提供商账户已配置。" } });
    globalThis.fetch = mockFetchResponse(409, {
      error: "already_exists",
      message: "This provider account is already configured.",
      message_key: "adminError.providerAlreadyConfigured",
    });

    try {
      await expect(api.createAgentCliProvider("ollama", {
        base_url: "http://localhost:11434",
      })).rejects.toThrow("此提供商账户已配置。");
    } finally {
      primeTranslations(en.panel);
    }
  });

  it("localizes keyed messages returned inside successful response bodies", () => {
    primeTranslations({ adminError: { providerApiKeyRequired: "请输入 API 密钥。" } });
    try {
      expect(localizedApiMessage(
        "Enter your API key.",
        "adminError.providerApiKeyRequired",
      )).toBe("请输入 API 密钥。");
    } finally {
      primeTranslations(en.panel);
    }
  });
});

describe("Home Assistant authentication", () => {
  it("delegates authenticated requests to Home Assistant", async () => {
    const fetchWithAuth = vi.fn((path: string, init?: RequestInit) => fetch(path, init));
    const refreshAccessToken = vi.fn();
    setHass({ fetchWithAuth, auth: { refreshAccessToken } });
    globalThis.fetch = mockFetchResponse(200, []);

    await api.listTokens();
    expect(fetchWithAuth).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/tokens",
      expect.objectContaining({ method: "GET" }),
    );
    expect(refreshAccessToken).not.toHaveBeenCalled();
  });

  it("does not probe an admin endpoint when no HA session is available", async () => {
    setHass(null);
    globalThis.fetch = mockFetchResponse(200, []);

    await expect(api.listTokens()).rejects.toThrow(ApiError);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("api.getEntityTree", () => {
  it("adds force_reload query param when requested", async () => {
    globalThis.fetch = mockFetchResponse(200, {});

    await api.getEntityTree(true);
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/entities?force_reload=1");
  });

  it("omits force_reload by default", async () => {
    globalThis.fetch = mockFetchResponse(200, {});

    await api.getEntityTree();
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/entities");
  });
});

describe("api version history", () => {
  it("listVersions builds the recent-feed query", async () => {
    globalThis.fetch = mockFetchResponse(200, { versions: [], total: 0 });
    await api.listVersions({ limit: 100 });
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/versions?limit=100");
    expect(opts.method).toBe("GET");
  });

  it("listVersions scopes to a resource", async () => {
    globalThis.fetch = mockFetchResponse(200, { versions: [], total: 0 });
    await api.listVersions({ resource_type: "automation", resource_id: "phx_x" });
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/versions?resource_type=automation&resource_id=phx_x");
  });

  it("getVersion fetches a single record", async () => {
    globalThis.fetch = mockFetchResponse(200, { id: "v1" });
    await api.getVersion("v1");
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/versions/v1");
  });

  it("restoreVersion POSTs to the restore path", async () => {
    globalThis.fetch = mockFetchResponse(200, { restored: true });
    await api.restoreVersion("v1");
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/versions/v1/restore");
    expect(opts.method).toBe("POST");
  });
});

describe("api.clearMesaOrphans", () => {
  it("POSTs to the orphans clear endpoint", async () => {
    const body = { deleted: { entities: ["light.gone"], areas: [], integrations: [] }, count: 1 };
    globalThis.fetch = mockFetchResponse(200, body);

    const result = await api.clearMesaOrphans();
    expect(result).toEqual(body);
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/orphans/clear");
    expect(opts.method).toBe("POST");
  });
});

describe("api MESA suggestions", () => {
  it("dismiss POSTs the key", async () => {
    const body = { dismissed: "naked_risky:entity:lock.front", suggestions: [], dismissed_suggestions: ["naked_risky:entity:lock.front"] };
    globalThis.fetch = mockFetchResponse(200, body);

    const result = await api.dismissMesaSuggestion("naked_risky:entity:lock.front");
    expect(result).toEqual(body);
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/suggestions/dismiss");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ key: "naked_risky:entity:lock.front" });
  });

  it("restore POSTs all: true", async () => {
    globalThis.fetch = mockFetchResponse(200, { restored: 2, suggestions: [], dismissed_suggestions: [] });

    await api.restoreMesaSuggestions({ all: true });
    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/phoenix-mcp/admin/mesa/suggestions/restore");
    expect(JSON.parse(opts.body)).toEqual({ all: true });
  });
});
