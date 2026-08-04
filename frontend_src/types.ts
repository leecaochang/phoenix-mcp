export type NodeState = "GREY" | "YELLOW" | "GREEN" | "RED";
export type Permission = "WRITE" | "READ" | "DENY" | "NO_ACCESS" | "NOT_FOUND";
export type Outcome =
  | "allowed"
  | "denied"
  | "not_found"
  | "rate_limited"
  | "not_implemented"
  | "invalid_request"
  | "pending_approval";

export type CapMode = "deny" | "allow" | "confirm";
export type Persona =
  | "new_user"
  | "read_only"
  | "voice_assistant"
  | "dashboard_designer"
  | "maintenance"
  | "automation_builder"
  | "power_user"
  | "home_admin"
  | "esphome"
  | "custom";

export type CapTier = "read" | "everyday" | "config_write" | "system" | "irreversible";

export interface CapabilityFlagsMap {
  cap_config_read: CapMode;
  cap_template_render: CapMode;
  cap_log_read: CapMode;
  cap_search: CapMode;
  cap_registry_read: CapMode;
  cap_traces: CapMode;
  cap_diagnostics: CapMode;
  cap_broadcast: CapMode;
  cap_service_response: CapMode;
  cap_automation_write: CapMode;
  cap_script_write: CapMode;
  cap_blueprint_write: CapMode;
  cap_scene_write: CapMode;
  cap_helper_write: CapMode;
  cap_physical_control: CapMode;
  cap_restart: CapMode;
  cap_integration_write: CapMode;
  cap_lovelace_write: CapMode;
  cap_registry_write: CapMode;
  cap_radio_write: CapMode;
  cap_energy_write: CapMode;
  cap_backup: CapMode;
  cap_filesystem: CapMode;
  cap_yaml_edit: CapMode;
  cap_esphome_yaml: CapMode;
  cap_esphome_flash: CapMode;
}

export type CapName = keyof CapabilityFlagsMap;

export interface PermissionNode {
  state: NodeState;
  hint: string | null;
}

export interface PermissionTree {
  domains: Record<string, PermissionNode>;
  devices: Record<string, PermissionNode>;
  entities: Record<string, PermissionNode>;
}

// A named snapshot of a token's full settings (workspace model): the active
// preset absorbs live edits when the admin switches to another preset.
export interface TokenPreset {
  id: string;
  name: string;
  created_at: string | null;
  caps: Record<CapName, CapMode>;
  persona: Persona;
  permissions: PermissionTree;
  pass_through: boolean;
  use_assist_exposure: boolean;
  announce_all_tools: boolean;
  confirm_inline_wait_seconds: number;
  rate_limit_requests: number;
  rate_limit_burst: number;
}

export interface TokenRecord extends CapabilityFlagsMap {
  id: string;
  name: string;
  created_at: string;
  created_by: string;
  expires_at: string | null;
  revoked: boolean;
  last_used_at: string | null;
  updated_at: string | null;
  pass_through: boolean;
  use_assist_exposure?: boolean;
  announce_all_tools?: boolean;
  confirm_inline_wait_seconds: number;
  persona: Persona;
  rate_limit_requests: number;
  rate_limit_burst: number;
  permissions: PermissionTree;
  presets: TokenPreset[];
  active_preset_id: string | null;
}

export interface TokenCreateResponse extends TokenRecord {
  token: string;
}

export interface ArchivedTokenRecord {
  id: string;
  name: string;
  created_at: string;
  created_by: string;
  revoked_at: string;
  revoked: boolean;
  expires_at: string | null;
  last_used_at: string | null;
}

export interface GlobalSettings {
  kill_switch: boolean;
  disable_all_logging: boolean;
  log_allowed: boolean;
  log_denied: boolean;
  log_rate_limited: boolean;
  log_entity_names: boolean;
  log_client_ip: boolean;
  notify_on_rate_limit: boolean;
  notify_on_approval: boolean;
  audit_flush_interval: number;
  audit_log_maxlen: number;
  mesa_mode: MesaMode;
  mesa_inject_enabled: boolean;
  token_presets_enabled: boolean;
  agentcli_scrollback_lines: number;
  agentcli_max_iterations: number;
  agentcli_global: boolean;
  // Assist bridge: the token id whose scoped tools HA's native Assist/voice
  // pipeline resolves against (null = unbound). Writable.
  assist_bound_token_id?: string | null;
  // Read-only: whether the running HA exposes the llm.API seam (server-computed,
  // not persisted). The Assist bind toggle is disabled when false.
  assist_api_supported?: boolean;
  // Phoenix MCP voice agent: Phoenix MCP registered as HA's own conversation agent, running its
  // own model on the chosen token's scope. Independent of the Assist bridge above.
  voice_agent_enabled?: boolean;
  voice_agent_token_id?: string | null;
  voice_agent_provider_id?: string | null;
  voice_agent_model?: string | null;
  // Id of the Assist pipeline Phoenix MCP created via the one-click setup (null when set up
  // manually or not run). Read-only from the panel's perspective (set by the helper).
  voice_agent_pipeline_id?: string | null;
  // Read-only: whether the running HA exposes the Assist pipeline seam (one-click).
  voice_agent_pipeline_supported?: boolean;
  // Phoenix MCP AI Task entity: Phoenix MCP registered as an HA AITaskEntity running its own model
  // on the chosen token's scope. Independent of the voice agent and the bridge.
  ai_task_enabled?: boolean;
  ai_task_token_id?: string | null;
  ai_task_provider_id?: string | null;
  ai_task_model?: string | null;
  // Read-only: whether the running HA exposes the AI Task entity platform.
  ai_task_supported?: boolean;
  // Read-only: which ESPHome surfaces exist on this system (server-computed).
  // The panel marks the ESPHome capability and persona as inapplicable when they
  // are absent; the tools themselves are hidden from tools/list server-side.
  esphome_integration?: boolean;
  esphome_builder?: boolean;
  esphome_builder_live?: boolean;
}

// Status of HA's "Data generation tasks" default entity vs Phoenix MCP's AI Task entity.
export interface AiTaskPreferredStatus {
  supported: boolean;
  entity_id: string | null;          // Phoenix MCP's AI Task entity (null until configured)
  gen_data_entity_id: string | null; // the current default data-gen entity
  gen_data_name: string | null;      // its friendly name (for the overwrite warning)
  is_preferred: boolean;             // Phoenix MCP is the current default
}

export type AgentCliProviderKind =
  "claude" | "deepseek" | "chatgpt" | "gemini" | "grok" | "kimi" | "meta"
  | "minimax" | "openrouter" | "nvidia" | "ollama" | "ollama_cloud";

// One configured provider account. Multiple of the same kind are allowed (e.g.
// two Claude keys, two Ollama servers); `name` disambiguates duplicates.
export interface AgentCliInstance {
  id: string;
  kind: AgentCliProviderKind;
  name: string;
  model: string;
  base_url?: string;
}

export type MesaMode = "off" | "advisory" | "enforced";

// The inheritance levels a profile can be authored at, most specific first.
// ONE definition: the editor, its label maps, the list table and the in-context
// injector all derive from this, because they each used to carry their own copy
// and a scope missing from one of them failed silently rather than at compile
// time. Pinned against the backend's own list by a contract fixture.
// Deployment defaults are not a keyed level and are handled separately.
export type MesaProfileScope = "entity" | "device" | "area" | "integration" | "domain";

// A MESA profile document (root form): the shape mesa-core serialises and
// accepts. Kept loose because the kernel is a small fixed subset of a larger
// optional schema that Phoenix MCP does not re-specify on the frontend.
export interface MesaProfileDocument {
  semantic_profile?: Record<string, unknown>;
  privacy_classification?: Record<string, unknown>;
  // Provenance. "developer" marks a vendor-supplied profile imported from an
  // integration's mesa_profile.json sidecar; "user" is panel-authored.
  metadata_origin?: { source?: string };
}

export interface MesaProfileListItem {
  entity_id: string;
  document: MesaProfileDocument;
}

export interface MesaProfilesResponse {
  profiles: MesaProfileListItem[];
  total_matched: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface MesaProfileDetail {
  entity_id: string;
  stored: MesaProfileDocument | null;
  effective: MesaProfileDocument;
  explanation: {
    entity_id: string;
    explanation: Array<{
      field_path: string;
      effective_value: unknown;
      provided_by_level: string;
      provided_by_origin: string;
      conflict: boolean;
    }>;
    conflicts_detected: boolean;
    warnings: string[];
  };
}

export interface MesaValidationIssue {
  entity_id: string;
  declared_value: string;
  automation_id: string;
  role: string;
  severity: string;
  recommendation: string;
}

// One suggested MESA profile from the admin-side scanner (never auto-applied).
export interface MesaSuggestion {
  key: string;
  signal: "blast_radius" | "naked_risky";
  scope: "entity" | "domain";
  subject_id: string;
  suggested_mode: string;
  reason: string;
  reason_key?: string;
  reason_params?: Record<string, string | number>;
  evidence: Record<string, unknown>;
}

export interface CardCatalogEntry {
  type: string;
  name: string | null;
  description: string | null;
  documentation_url: string | null;
  preview: boolean;
  available: boolean;
  has_visual_editor: boolean;
  stub_config: unknown;
  source: "picker" | "element";
}

export interface CardCatalogResponse {
  // False means no browser has reported yet, which is NOT the same as an
  // instance with no custom cards. Never render it as an empty catalog.
  harvested: boolean;
  harvested_at: string | null;
  entries: CardCatalogEntry[];
  resource_count: number;
  failed_imports: { url: string; error: string }[];
}

export interface MesaSuggestionActionResponse {
  suggestions: MesaSuggestion[];
  dismissed_suggestions: string[];
  dismissed?: string;
  restored?: string | number;
}

export interface MesaIssuesResponse {
  issues: MesaValidationIssue[];
  orphans: string[];
  orphan_devices: string[];
  orphan_areas: string[];
  orphan_integrations: string[];
  suggestions: MesaSuggestion[];
  dismissed_suggestions: string[];
}

export interface MesaPutResponse {
  entity_id: string;
  stored: MesaProfileDocument;
  warnings: MesaValidationIssue[];
}

// mesa-core portability archive (raw profile documents; shape owned by
// mesa-core, so the inner sections stay loosely typed).
export interface MesaExportArchive {
  mesa_export: {
    format_version: string;
    exported_at?: string;
    mesa_core_version?: string;
    entities?: Record<string, unknown>;
    devices?: Record<string, unknown>;
    domains?: Record<string, unknown>;
    integrations?: Record<string, unknown>;
    areas?: Record<string, unknown>;
    deployment_defaults?: Record<string, unknown>;
  };
}

export interface MesaImportResult {
  imported: number;
  overwritten: number;
  skipped_existing: string[];
  invalid: Record<string, string>;
}

export interface AuditEntry {
  request_id: string;
  timestamp: string;
  token_id: string;
  token_name: string;
  method: string;
  resource: string;
  outcome: Outcome;
  client_ip: string;
  pass_through: boolean;
  payload?: string | null;
  mesa_advisory?: boolean;
  preset?: string | null;
  stale_tools_advisory?: boolean;
}

export interface EntityInfo {
  entity_id: string;
  friendly_name: string | null;
  device_id: string | null;
  area_id: string | null;
  area_name: string | null;
  labels: { id: string; name: string }[];
}

export interface DeviceInfo {
  device_id: string;
  name: string;
  area_id: string | null;
  area_name: string | null;
  entities: string[];
}

export interface DomainTree {
  devices: Record<string, DeviceInfo>;
  deviceless_entities: string[];
  entity_details: Record<string, EntityInfo>;
}

export type EntityTree = Record<string, DomainTree>;

export interface ResolutionStep {
  level: string;
  state: string;
}

export interface ResolveResult {
  entity_id: string;
  resolution_path: ResolutionStep[];
  effective: Permission;
  effective_hint: string | null;
}

export interface TokenConnection {
  last_used_at: string | null;
  request_count: number;
}

export interface TokenStats {
  token_id: string;
  token_name: string;
  request_count: number;
  denied_count: number;
  rate_limit_hits: number;
  last_used_at: string | null;
  status: string;
}

export interface ScopeResult {
  token_id: string;
  token_name: string;
  readable: string[];
  writable: string[];
  persona: Persona;
  capability_flags: CapabilityFlagsMap;
}

export interface CreateTokenBody {
  name: string;
  expires_at?: string;
  pass_through?: boolean;
  confirm_pass_through?: boolean;
  rate_limit_requests?: number;
  rate_limit_burst?: number;
}

export interface PatchTokenBody {
  name?: string;
  pass_through?: boolean;
  confirm_pass_through?: boolean;
  rate_limit_requests?: number;
  rate_limit_burst?: number;
  persona?: Persona;
  cap_automation_write?: CapMode;
  cap_script_write?: CapMode;
  cap_blueprint_write?: CapMode;
  cap_log_read?: CapMode;
  cap_config_read?: CapMode;
  cap_template_render?: CapMode;
  cap_restart?: CapMode;
  cap_physical_control?: CapMode;
  cap_service_response?: CapMode;
  cap_broadcast?: CapMode;
  cap_search?: CapMode;
  cap_registry_read?: CapMode;
  cap_traces?: CapMode;
  cap_diagnostics?: CapMode;
  cap_scene_write?: CapMode;
  cap_helper_write?: CapMode;
  cap_integration_write?: CapMode;
  cap_lovelace_write?: CapMode;
  cap_registry_write?: CapMode;
  cap_radio_write?: CapMode;
  cap_energy_write?: CapMode;
  cap_backup?: CapMode;
  cap_filesystem?: CapMode;
  cap_yaml_edit?: CapMode;
  cap_esphome_yaml?: CapMode;
  cap_esphome_flash?: CapMode;
  use_assist_exposure?: boolean;
  announce_all_tools?: boolean;
  confirm_inline_wait_seconds?: number;
}

export interface PermissionPatchBody {
  state: NodeState;
  hint?: string | null;
}

export interface AuditQueryParams {
  limit?: number;
  offset?: number;
  token_id?: string;
  outcome?: string;
  ip?: string;
  method?: string;
  resource?: string;
  /** ISO timestamp: only entries at or after this time. */
  since?: string;
}

export interface AuditListResponse {
  entries: AuditEntry[];
  total: number;
}

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "cancelled";

export interface ApprovalDiff {
  kind?: "yaml_diff" | "config_diff" | "service_preview" | "system_action" | "file_write"
    | "esphome_yaml";
  summary?: string;
  /** Catalog key + params for `summary`, so the panel can render it localized.
   *  Absent on records written before this existed, and on any key this bundle
   *  does not know; both fall back to the stored English `summary`. Plain
   *  strings, not a key union: they cross a version boundary. */
  summary_key?: string;
  summary_params?: Record<string, string | number>;
  target?: { type?: string; id?: string | null; label?: string | null };
  before?: string | null;
  after?: string | null;
  preview?: Record<string, unknown>;
}

/** Result of approving several approvals in one admin action.
 *
 * Home Assistant writes are not transactional, so a batch cannot be atomic. The
 * server stops at the first failure rather than ploughing on, because the likely
 * failures are systematic (a capability revoked mid-batch, the kill switch, a
 * stale hash after an earlier item touched the same file) and continuing would
 * reproduce the same error for every remaining item. `remaining` is therefore not
 * an error list: those are untouched and still individually approvable.
 */
export interface BatchApproveResult {
  applied: { approval_id: string; tool_name: string | null }[];
  failed: {
    approval_id: string;
    tool_name: string | null;
    status: number;
    error: string;
    message?: string | null;
    message_key?: string;
    message_params?: Record<string, unknown>;
  } | null;
  remaining: string[];
}

export interface ApprovalRecord {
  id: string;
  token_id: string;
  token_name: string;
  tool_name: string;
  cap_name: string;
  args: Record<string, unknown>;
  diff: ApprovalDiff;
  status: ApprovalStatus;
  created_at: string;
  expires_at: string;
  resolved_at: string | null;
  approved_by_user_id: string | null;
  rejected_reason: string | null;
  result: unknown | null;
  request_id: string;
  client_ip: string | null;
  /** Its saved action is executing right now (server-side claim). Present on the
   *  pending list so a panel loaded mid-execution renders it non-actionable too,
   *  not only one that happened to receive the claim event. */
  in_progress?: boolean;
}

export interface ApprovalListResponse {
  approvals: ApprovalRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApprovalListParams {
  status?: ApprovalStatus;
  token_id?: string;
  limit?: number;
  offset?: number;
}

export type VersionAction = "create" | "edit" | "delete" | "rollback";
// Must list every resource_type _record_version is called with in mcp_view.py.
// blueprint, entity and esphome_yaml were all recorded by the backend while
// missing here, so a version of those types was mistyped at every use; the same
// drift previously hid "esphome_yaml" from ApprovalDiff.kind, which routed those
// approvals to the wrong renderer.
export type VersionResourceType =
  | "automation"
  | "script"
  | "scene"
  | "helper"
  | "dashboard"
  | "blueprint"
  | "entity"
  | "yaml_config"
  | "esphome_yaml"
  | "energy"
  | "file"
  | "config_entry";

export interface VersionSummary {
  id: string;
  resource_type: VersionResourceType;
  resource_id: string;
  alias: string | null;
  action: VersionAction;
  token_id: string | null;
  token_name: string | null;
  approved_by_user_id: string | null;
  timestamp: string;
  // One-line what-changed description (older records have none), plus the
  // catalog key + params that produced it so the Changes tab can localize it.
  summary?: string | null;
  summary_key?: string | null;
  summary_params?: Record<string, string | number> | null;
  has_before: boolean;
  has_after: boolean;
}

export interface VersionRecord {
  id: string;
  resource_type: VersionResourceType;
  resource_id: string;
  alias: string | null;
  action: VersionAction;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  token_id: string | null;
  token_name: string | null;
  request_id: string | null;
  approved_by_user_id: string | null;
  timestamp: string;
  summary?: string | null;
  summary_key?: string | null;
  summary_params?: Record<string, string | number> | null;
}

export interface VersionListResponse {
  resource_type: string | null;
  resource_id: string | null;
  versions: VersionSummary[];
  total: number;
}

export interface VersionRestoreResponse {
  restored: boolean;
  version_id: string;
  resource_type: string;
  resource_id: string;
}

declare global {
  namespace React.JSX {
    interface IntrinsicElements {
      "ha-card": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        header?: string;
        outlined?: boolean;
      };
      "ha-switch": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        checked?: boolean;
        disabled?: boolean;
      };
      "ha-icon": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        icon?: string;
      };
      "ha-icon-button": React.DetailedHTMLProps<React.ButtonHTMLAttributes<HTMLElement>, HTMLElement> & {
        label?: string;
        disabled?: boolean;
      };
      "ha-circular-progress": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        active?: boolean;
        size?: string;
      };
      "ha-menu-button": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "ha-code-editor": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "hui-card": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
    }
  }
}
