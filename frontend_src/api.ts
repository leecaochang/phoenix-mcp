import type {
  AgentCliProviderKind,
  AgentCliInstance,
  AgentCliProvidersResponse,
  AiTaskPreferredStatus,
  ApprovalListParams,
  ApprovalListResponse,
  ApprovalRecord,
  BulkPermissionResult,
  BatchApproveResult,
  AuditEntry,
  AuditListResponse,
  AuditQueryParams,
  CardCatalogResponse,
  ArchivedTokenRecord,
  CreateTokenBody,
  DeclaredModelCaps,
  EntityTree,
  GlobalSettings,
  IntegrationPermissionOption,
  MesaExportArchive,
  MesaImportResult,
  MesaIssuesResponse,
  MesaSuggestionActionResponse,
  MesaProfileDetail,
  MesaProfileDocument,
  MesaProfilesResponse,
  MesaPutResponse,
  PatchTokenBody,
  PermissionPatchBody,
  PermissionTree,
  ResolveResult,
  ScopeResult,
  TokenConnection,
  TokenCreateResponse,
  TokenRecord,
  TokenStats,
  VersionListResponse,
  VersionRecord,
  VersionRestoreResponse,
} from "./types";

import { hasMessage, t } from "./i18n";

const BASE = "/api/phoenix-mcp/admin";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let hassInstance: any = null;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function setHass(hass: any) {
  hassInstance = hass;
}

// The hass to authenticate with. Normally the one passed to setHass, but the
// in-context inject modal can run in a module realm where setHass was never
// called (a second injector copy, or one stood down by the singleton guard), so
// fall back to the live hass on the page's <home-assistant> element. Without this
// a request would go out with no Authorization header and HA would 401 + ban-log
// it. Harmless for the panel, which reads the same object.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function authHass(): any {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const live = (document.querySelector("home-assistant") as any)?.hass;
    if (typeof live?.fetchWithAuth === "function") return live;
  } catch { /* use the object most recently supplied by the panel */ }
  return hassInstance;
}

// The live hass object, for components that hand it to an HA custom element
// (the dashboard preview's <hui-card>). Unlike the hass prop drilled through
// React (which only re-renders on user change), this is fresh on every read:
// setHass runs on every hass assignment from HA.
export function currentHass(): unknown {
  return authHass();
}

export function localizedApiMessage(
  message: string,
  messageKey?: string,
  messageParams?: Record<string, string | number>,
): string {
  return messageKey && hasMessage(messageKey) ? t(messageKey, messageParams) : message;
}

class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string, messageKey?: string, messageParams?: Record<string, string | number>) {
    // `message` is the English the backend sent and stays the fallback: it is
    // what every un-migrated endpoint returns, and what an older backend
    // returns to a newer panel. A key, when present, localizes it.
    super(localizedApiMessage(message, messageKey, messageParams));
    this.status = status;
    this.code = code;
  }
}

async function _doReq<T>(method: string, path: string, body?: unknown): Promise<T> {
  const hass = authHass();
  if (typeof hass?.fetchWithAuth !== "function") {
    throw new ApiError(401, "unauthorized", t("common.noSession"));
  }
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const opts: RequestInit = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);

  // HA owns access-token refresh and request authentication. Calling
  // refreshAccessToken here races its WebSocket/session lifecycle in browsers.
  const res = await hass.fetchWithAuth(`${BASE}${path}`, opts);

  if (res.status === 204) return undefined as T;
  const json = await res.json().catch(() => ({ error: "parse_error", message: res.statusText }));
  if (!res.ok) {
    throw new ApiError(res.status, json.error ?? "unknown", json.message ?? res.statusText,
                       json.message_key, json.message_params);
  }
  return json as T;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  return _doReq<T>(method, path, body);
}

function buildQuery(params?: AuditQueryParams): string {
  if (!params) return "";
  const p = new URLSearchParams();
  if (params.limit !== undefined) p.set("limit", String(params.limit));
  if (params.offset !== undefined) p.set("offset", String(params.offset));
  if (params.token_id) p.set("token_id", params.token_id);
  if (params.outcome) p.set("outcome", params.outcome);
  if (params.ip) p.set("ip", params.ip);
  if (params.method) p.set("method", params.method);
  if (params.resource) p.set("resource", params.resource);
  if (params.since) p.set("since", params.since);
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  listTokens: () => req<TokenRecord[]>("GET", "/tokens"),
  getToken: (id: string) => req<TokenRecord>("GET", `/tokens/${id}`),
  createToken: (body: CreateTokenBody) =>
    req<TokenCreateResponse>("POST", "/tokens", body),
  patchToken: (id: string, body: PatchTokenBody) =>
    req<TokenRecord>("PATCH", `/tokens/${id}`, body),
  revokeToken: (id: string) => req<void>("DELETE", `/tokens/${id}`),
  rotateToken: (id: string) => req<TokenCreateResponse>("POST", `/tokens/${id}/rotate`),

  createPreset: (tokenId: string, name: string) =>
    req<TokenRecord>("POST", `/tokens/${tokenId}/presets`, { name }),
  renamePreset: (tokenId: string, presetId: string, name: string) =>
    req<TokenRecord>("PATCH", `/tokens/${tokenId}/presets/${presetId}`, { name }),
  deletePreset: (tokenId: string, presetId: string) =>
    req<TokenRecord>("DELETE", `/tokens/${tokenId}/presets/${presetId}`),
  applyPreset: (tokenId: string, presetId: string, confirmPassThrough = false) =>
    req<TokenRecord>(
      "POST",
      `/tokens/${tokenId}/presets/${presetId}/apply`,
      confirmPassThrough ? { confirm_pass_through: true } : {},
    ),

  listArchivedTokens: () => req<ArchivedTokenRecord[]>("GET", "/tokens/archived"),
  deleteArchivedToken: (id: string) => req<void>("DELETE", `/tokens/archived/${id}`),

  getPermissions: (id: string) => req<PermissionTree>("GET", `/tokens/${id}/permissions`),
  setPermissions: (id: string, tree: PermissionTree) =>
    req<PermissionTree>("PUT", `/tokens/${id}/permissions`, tree),
  patchDomainPermission: (tokenId: string, domain: string, body: PermissionPatchBody) =>
    req<PermissionTree>("PATCH", `/tokens/${tokenId}/permissions/domains/${encodeURIComponent(domain)}`, body),
  patchDevicePermission: (tokenId: string, deviceId: string, body: PermissionPatchBody) =>
    req<PermissionTree>("PATCH", `/tokens/${tokenId}/permissions/devices/${encodeURIComponent(deviceId)}`, body),
  patchEntityPermission: (tokenId: string, entityId: string, body: PermissionPatchBody) =>
    req<PermissionTree>("PATCH", `/tokens/${tokenId}/permissions/entities/${encodeURIComponent(entityId)}`, body),
  getPermissionIntegrationOptions: (tokenId: string) =>
    req<{ integrations: IntegrationPermissionOption[] }>(
      "GET", `/tokens/${tokenId}/permissions/integration-options`,
    ),
  bulkSelectPermissions: (
    tokenId: string,
    selectorType: "area" | "label" | "integration",
    selectorId: string,
    state: PermissionPatchBody["state"],
  ) => req<BulkPermissionResult>(
    "POST",
    `/tokens/${tokenId}/permissions/bulk-select`,
    { selector_type: selectorType, selector_id: selectorId, state },
  ),

  resolve: (tokenId: string, entityId: string) =>
    req<ResolveResult>("GET", `/tokens/${tokenId}/resolve/${encodeURIComponent(entityId)}`),
  getScope: (tokenId: string) => req<ScopeResult>("GET", `/tokens/${tokenId}/scope`),

  getEntityTree: (forceReload = false) =>
    req<EntityTree>("GET", `/entities${forceReload ? "?force_reload=1" : ""}`),

  getEntityHints: () =>
    req<{ entity_hints: Record<string, string> }>("GET", "/entity-hints"),
  setEntityHint: (entityId: string, hint: string | null) =>
    req<{ entity_hints: Record<string, string> }>("PUT", `/entity-hints/${encodeURIComponent(entityId)}`, { hint }),

  getTokenStats: (tokenId: string) => req<TokenStats>("GET", `/tokens/${tokenId}/stats`),
  getTokenConnection: (tokenId: string) => req<TokenConnection>("GET", `/tokens/${tokenId}/connection`),
  getTokenAudit: (tokenId: string, params?: AuditQueryParams) =>
    req<AuditEntry[]>("GET", `/tokens/${tokenId}/audit${buildQuery(params)}`),
  getAudit: (params?: AuditQueryParams) =>
    req<AuditListResponse>("GET", `/audit${buildQuery(params)}`),

  getInfo: () => req<{ version: string; min_ha_version: string; github_url: string }>("GET", "/info"),

  getSettings: () => req<GlobalSettings>("GET", "/settings"),
  patchSettings: (body: Partial<GlobalSettings>) =>
    req<GlobalSettings>("PATCH", "/settings", body),

  // One-click: create/remove an Assist pipeline pointed at Phoenix MCP's voice agent.
  createVoiceAgentPipeline: (preferred: boolean) =>
    req<{ pipeline_id: string; name: string; preferred: boolean }>(
      "POST", "/voice_agent/pipeline", { preferred },
    ),
  deleteVoiceAgentPipeline: () =>
    req<{ ok: boolean }>("DELETE", "/voice_agent/pipeline"),

  // The AI Task "Data generation tasks" default entity (one-click set/clear/status).
  getAiTaskPreferred: () =>
    req<AiTaskPreferredStatus>("GET", "/ai_task/preferred"),
  setAiTaskPreferred: () =>
    req<AiTaskPreferredStatus>("POST", "/ai_task/preferred"),
  clearAiTaskPreferred: () =>
    req<AiTaskPreferredStatus>("DELETE", "/ai_task/preferred"),

  wipe: (scope?: { wipe_core?: boolean; wipe_providers?: boolean; wipe_mesa?: boolean }) =>
    req<void>("DELETE", "/wipe", { confirm: "WIPE", ...(scope ?? {}) }),

  listApprovals: (params?: ApprovalListParams) => {
    const p = new URLSearchParams();
    if (params?.status) p.set("status", params.status);
    if (params?.token_id) p.set("token_id", params.token_id);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    const q = p.toString();
    return req<ApprovalListResponse>("GET", `/approvals${q ? `?${q}` : ""}`);
  },
  listMesaProfiles: (params?: { domain?: string; tag?: string; area?: string; origin?: string; limit?: number; cursor?: string }) => {
    const p = new URLSearchParams();
    if (params?.domain) p.set("domain", params.domain);
    if (params?.tag) p.set("tag", params.tag);
    if (params?.area) p.set("area", params.area);
    if (params?.origin) p.set("origin", params.origin);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.cursor) p.set("cursor", params.cursor);
    const q = p.toString();
    return req<MesaProfilesResponse>("GET", `/mesa/profiles${q ? `?${q}` : ""}`);
  },
  getMesaProfile: (entityId: string) =>
    req<MesaProfileDetail>("GET", `/mesa/profiles/${encodeURIComponent(entityId)}`),
  putMesaProfile: (entityId: string, doc: MesaProfileDocument) =>
    req<MesaPutResponse>("PUT", `/mesa/profiles/${encodeURIComponent(entityId)}`, doc),
  deleteMesaProfile: (entityId: string) =>
    req<{ entity_id: string; deleted: boolean }>("DELETE", `/mesa/profiles/${encodeURIComponent(entityId)}`),

  listMesaDomains: () =>
    req<{ domains: { domain: string; document: MesaProfileDocument }[] }>("GET", "/mesa/domains"),
  getMesaDomain: (domain: string) =>
    req<{ domain: string; stored: MesaProfileDocument | null }>("GET", `/mesa/domains/${encodeURIComponent(domain)}`),
  putMesaDomain: (domain: string, doc: MesaProfileDocument) =>
    req<{ domain: string; stored: MesaProfileDocument }>("PUT", `/mesa/domains/${encodeURIComponent(domain)}`, doc),
  deleteMesaDomain: (domain: string) =>
    req<{ domain: string; deleted: boolean }>("DELETE", `/mesa/domains/${encodeURIComponent(domain)}`),

  listMesaIntegrations: () =>
    req<{ integrations: { integration: string; document: MesaProfileDocument }[] }>("GET", "/mesa/integrations"),
  getMesaIntegration: (integration: string) =>
    req<{ integration: string; stored: MesaProfileDocument | null }>("GET", `/mesa/integrations/${encodeURIComponent(integration)}`),
  putMesaIntegration: (integration: string, doc: MesaProfileDocument) =>
    req<{ integration: string; stored: MesaProfileDocument }>("PUT", `/mesa/integrations/${encodeURIComponent(integration)}`, doc),
  deleteMesaIntegration: (integration: string) =>
    req<{ integration: string; deleted: boolean }>("DELETE", `/mesa/integrations/${encodeURIComponent(integration)}`),
  listMesaDevices: () =>
    req<{ devices: { device_id: string; document: MesaProfileDocument }[] }>("GET", "/mesa/devices"),
  getMesaDevice: (deviceId: string) =>
    req<{ device_id: string; stored: MesaProfileDocument | null }>("GET", `/mesa/devices/${encodeURIComponent(deviceId)}`),
  putMesaDevice: (deviceId: string, doc: MesaProfileDocument) =>
    req<{ device_id: string; stored: MesaProfileDocument }>("PUT", `/mesa/devices/${encodeURIComponent(deviceId)}`, doc),
  deleteMesaDevice: (deviceId: string) =>
    req<{ device_id: string; deleted: boolean }>("DELETE", `/mesa/devices/${encodeURIComponent(deviceId)}`),
  // A device id is an opaque registry id, so unlike areas the picker cannot show
  // the key itself and needs the display name Home Assistant shows.
  getMesaDeviceOptions: () =>
    req<{ devices: { id: string; name: string }[] }>("GET", "/mesa/device-options"),

  getMesaIntegrationOptions: () =>
    req<{ integrations: { id: string; name: string }[] }>("GET", "/mesa/integration-options"),

  listMesaAreas: () =>
    req<{ areas: { area_id: string; document: MesaProfileDocument }[] }>("GET", "/mesa/areas"),
  getMesaArea: (areaId: string) =>
    req<{ area_id: string; stored: MesaProfileDocument | null }>("GET", `/mesa/areas/${encodeURIComponent(areaId)}`),
  putMesaArea: (areaId: string, doc: MesaProfileDocument) =>
    req<{ area_id: string; stored: MesaProfileDocument }>("PUT", `/mesa/areas/${encodeURIComponent(areaId)}`, doc),
  deleteMesaArea: (areaId: string) =>
    req<{ area_id: string; deleted: boolean }>("DELETE", `/mesa/areas/${encodeURIComponent(areaId)}`),

  getMesaVocabulary: () =>
    req<{ canonical_tags: string[]; canonical_roots: string[] }>("GET", "/mesa/vocabulary"),

  // refresh=true recomputes everything (trigger issues, orphans, suggestions);
  // refresh="suggestions" recomputes only suggestions, leaving the separate
  // issues/orphans banner untouched (the Suggestions card's own Rescan).
  getMesaIssues: (refresh: boolean | "suggestions" = false) =>
    req<MesaIssuesResponse>(
      "GET",
      `/mesa/issues${refresh === "suggestions" ? "?refresh=suggestions" : refresh ? "?refresh=1" : ""}`,
    ),
  clearMesaOrphans: () =>
    req<{ deleted: { entities: string[]; devices: string[]; areas: string[]; integrations: string[] }; count: number }>(
      "POST",
      "/mesa/orphans/clear",
    ),
  exportMesaProfiles: () => req<MesaExportArchive>("GET", "/mesa/export"),
  importMesaProfiles: (archive: MesaExportArchive, onConflict: "skip" | "overwrite") =>
    req<MesaImportResult>("POST", "/mesa/import", { archive, on_conflict: onConflict }),
  dismissMesaSuggestion: (key: string) =>
    req<MesaSuggestionActionResponse>("POST", "/mesa/suggestions/dismiss", { key }),
  restoreMesaSuggestions: (body: { key?: string; all?: boolean }) =>
    req<MesaSuggestionActionResponse>("POST", "/mesa/suggestions/restore", body),

  // The dashboard card catalog. The POST body is built by utils/card_harvest;
  // only a browser can see which custom cards exist (see that module for why).
  getCardCatalog: () => req<CardCatalogResponse>("GET", "/card_catalog"),
  postCardCatalog: (body: unknown) =>
    req<{ harvested: boolean; harvested_at: string | null; cards: number; available: number; failed_imports: number }>(
      "POST",
      "/card_catalog",
      body,
    ),

  getApproval: (id: string) => req<ApprovalRecord>("GET", `/approvals/${encodeURIComponent(id)}`),
  approveApproval: (id: string, body: { note?: string } = {}) =>
    req<ApprovalRecord>("POST", `/approvals/${encodeURIComponent(id)}/approve`, body),
  rejectApproval: (id: string, body: { reason?: string } = {}) =>
    req<ApprovalRecord>("POST", `/approvals/${encodeURIComponent(id)}/reject`, body),
  // Approves several pending approvals in one admin action. The server walks them
  // in the given order and STOPS at the first failure, so the result reports what
  // was applied, what failed, and what was left untouched and still pending.
  batchApproveApprovals: (ids: string[]) =>
    req<BatchApproveResult>("POST", "/approvals/batch/approve", { approval_ids: ids }),
  cancelApproval: (id: string) => req<void>("DELETE", `/approvals/${encodeURIComponent(id)}`),

  listVersions: (params?: { resource_type?: string; resource_id?: string; limit?: number; offset?: number }) => {
    const p = new URLSearchParams();
    if (params?.resource_type) p.set("resource_type", params.resource_type);
    if (params?.resource_id) p.set("resource_id", params.resource_id);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    const q = p.toString();
    return req<VersionListResponse>("GET", `/versions${q ? `?${q}` : ""}`);
  },
  getVersion: (id: string) => req<VersionRecord>("GET", `/versions/${encodeURIComponent(id)}`),
  restoreVersion: (id: string, side?: "before" | "after") =>
    req<VersionRestoreResponse>("POST", `/versions/${encodeURIComponent(id)}/restore`, side ? { side } : undefined),

  // agentCLI: provider accounts live on the admin surface. Multiple accounts of
  // the same kind are allowed; each has an opaque instance id.
  getAgentCliProviders: () =>
    req<AgentCliProvidersResponse>("GET", "/agentcli/providers"),
  createAgentCliProvider: (
    kind: AgentCliProviderKind,
    body: { api_key?: string; base_url?: string; endpoint_id?: string; model?: string },
  ) => req<{ instance: AgentCliInstance }>("POST", "/agentcli/providers", { kind, ...body }),
  refreshAgentCliProvider: (id: string) =>
    req<{ models: string[]; capabilities: Record<string, DeclaredModelCaps>; declared: boolean; checked_at: string }>(
      "POST", `/agentcli/providers/${encodeURIComponent(id)}/refresh`),
  probeAgentCliCapabilities: (id: string) =>
    req<{ model: string; probed: DeclaredModelCaps & { effort_levels?: string[] }; calls: number; checked_at: string; effort_checkable: boolean; answered: boolean }>(
      "POST", `/agentcli/providers/${encodeURIComponent(id)}/probe`),
  setAgentCliProviderModel: (id: string, model: string) =>
    req<{ instance: { id: string; model: string } }>(
      "PATCH", `/agentcli/providers/${encodeURIComponent(id)}`, { model }),
  deleteAgentCliProvider: (id: string) =>
    req<{ deleted: string }>("DELETE", `/agentcli/providers/${encodeURIComponent(id)}`),
  getAgentCliModels: (id: string) =>
    req<{ models: string[] }>("GET", `/agentcli/providers/${encodeURIComponent(id)}/models`),
  probeAgentCliProvider: (
    kind: AgentCliProviderKind,
    body: { api_key?: string; base_url?: string; endpoint_id?: string },
  ) => req<{
    ok: boolean;
    models: string[];
    error?: string;
    message_key?: string;
    message_params?: Record<string, string | number>;
    message_passthrough?: boolean;
  }>(
    "POST", "/agentcli/probe", { kind, ...body }),
};

// The agentCLI chat endpoint streams Server-Sent Events over a POST, so it
// cannot use EventSource (GET-only). Read the response body as a stream and
// parse event:/data: frames, invoking onEvent for each. Returns when the stream
// ends (the server's final `done` event) or is aborted.
export interface AgentCliChatBody {
  token_id: string;
  instance_id: string;
  model?: string;
  messages: unknown[];
  // The new user message. Omitted (with continue:true) when resuming a turn
  // that paused at the round-cap checkpoint: the model continues from the
  // existing conversation with no new user message.
  user?: string;
  continue?: boolean;
  // One-turn Home-focused override. Valid only alongside a new user message.
  home_focus_bypass?: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  options?: Record<string, any>;
}

export async function agentCliChat(
  body: AgentCliChatBody,
  onEvent: (name: string, payload: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  const hass = authHass();
  if (typeof hass?.fetchWithAuth !== "function") {
    throw new ApiError(401, "unauthorized", t("common.noSession"));
  }
  const res = await hass.fetchWithAuth("/api/phoenix-mcp/agentcli/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const j = await res.json().catch(() => ({ error: "stream_error", message: res.statusText }));
    throw new ApiError(res.status, j.error ?? "unknown", j.message ?? res.statusText,
                       j.message_key, j.message_params);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // Frames are separated by a blank line; each frame has `event:` and `data:` lines.
  const flush = (frame: string) => {
    let name = "message";
    const dataLines: string[] = [];
    for (const raw of frame.split("\n")) {
      const line = raw.replace(/\r$/, "");
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let payload: unknown = null;
    try { payload = JSON.parse(dataLines.join("\n")); } catch { payload = null; }
    onEvent(name, payload);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (frame.trim()) flush(frame);
    }
  }
  if (buffer.trim()) flush(buffer);
}

export { ApiError };
