import type { TokenRecord } from "./types";
import { localeDate, localeDateTime, t } from "./i18n";

export const HIGH_RISK_DOMAINS = new Set([
  "homeassistant", "recorder", "system_log", "hassio",
  "backup", "notify", "persistent_notification", "mqtt",
]);

// Mirrors const.py's MESA_APPROVED_EXECUTOR: the non-dispatchable executor key
// every MESA-gated confirm (whatever real tool triggered it - call_service,
// HassTurnOn, etc) is stored under as its approval's tool_name. Shown verbatim
// it reads like a real tool name a client could call, which it isn't - so any
// display of a tool_name or an "approval:<tool_name>:<id>" resource string maps
// it to a readable label instead.
const MESA_APPROVED_EXECUTOR = "call_service_mesa_approved";
const MESA_GATED_LABEL = () => t("common.mesaGatedAction");

/** Readable label for an approval's tool_name, humanizing the MESA sentinel. */
export function friendlyToolName(toolName: string): string {
  return toolName === MESA_APPROVED_EXECUTOR ? MESA_GATED_LABEL() : toolName;
}

/** Readable label for an audit entry's "approval:<tool_name>:<id>" resource
 *  string, humanizing an embedded MESA sentinel. Any other resource string
 *  passes through unchanged. */
export function friendlyResource(resource: string): string {
  // Defensive: a legacy poisoned audit entry could carry a non-string resource
  // (the backend now coerces at record() time, but old ring-buffer entries may
  // linger). Coerce so .startsWith and rendering never throw.
  const res = typeof resource === "string" ? resource : String(resource ?? "");
  const prefix = `approval:${MESA_APPROVED_EXECUTOR}:`;
  return res.startsWith(prefix) ? `approval:${MESA_GATED_LABEL()}:${res.slice(prefix.length)}` : res;
}

// Approval status slugs arrive raw from the server (both the approvals API and
// the Agent Chat stream send the stored value), so anything rendering one to
// the operator resolves it through the catalog first - a raw "approved" dropped
// into a translated sentence stays English in every locale. Unknown values pass
// through unchanged: Agent Chat parks its own already-translated in-flight
// markers ("approving...") in the same field.
const APPROVAL_STATUS_KEYS: Record<string, string> = {
  pending: "approvals.statusPending",
  approved: "approvals.statusApproved",
  failed: "approvals.reason.execution_failed",
  rejected: "approvals.statusRejected",
  expired: "approvals.statusExpired",
  cancelled: "approvals.statusCancelled",
  resolved: "approvals.statusResolved",
  execution_failed: "agentchat.executionFailed",
};

/** Readable label for an approval status slug; unknown values pass through. */
export function approvalStatusLabel(status: string): string {
  const key = APPROVAL_STATUS_KEYS[status];
  return key ? t(key) : status;
}

// Effort/thinking level slugs as the operator's language renders them. The chat
// dropdown and the settings card both name these, and a slug interpolated into a
// translated sentence stays English in every locale, so there is one map.
const EFFORT_LEVEL_KEYS: Record<string, string> = {
  off: "agentchat.levelOff", none: "agentchat.levelOff", on: "agentchat.levelOn",
  minimal: "agentchat.levelMinimal", low: "agentchat.levelLow",
  medium: "agentchat.levelMedium", high: "agentchat.levelHigh",
  xhigh: "agentchat.levelXHigh", max: "agentchat.levelMax",
};

export function effortLevelLabel(level: string): string {
  const key = EFFORT_LEVEL_KEYS[level];
  return key ? t(key) : level;
}

export function formatDate(iso: string | null): string {
  if (!iso) return t("common.never");
  return localeDate(iso);
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return t("common.never");
  return localeDateTime(iso);
}

export function tokenStatus(token: TokenRecord): string {
  if (token.revoked) return "Revoked";
  if (token.expires_at && new Date(token.expires_at) <= new Date()) return "Expired";
  return "Active";
}

/** Display labels for tokenStatus()'s stable internal values. */
// The resolver's Permission verdict, for display. The raw value is what the
// audit payload and the REST API carry; these are the labels beside it.
export const PERMISSION_LABEL_KEYS: Record<string, string> = {
  WRITE: "perms.stateWrite",
  READ: "perms.stateRead",
  DENY: "perms.stateDeny",
  NO_ACCESS: "perms.stateNoAccess",
  NOT_FOUND: "perms.stateNotFound",
};

export const TOKEN_STATUS_KEYS: Record<string, string> = {
  Active: "tokens.statusActive",
  Expired: "tokens.statusExpired",
  Revoked: "tokens.statusRevoked",
};

export function tokenStatusLabel(status: string): string {
  const key = TOKEN_STATUS_KEYS[status];
  return key ? t(key) : status;
}

export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Some HTTP contexts expose the API but reject it at call time. Try the
      // older document command before reporting that copying is unavailable.
    }
  }

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  try {
    ta.focus();
    ta.select();
    if (typeof document.execCommand !== "function" || !document.execCommand("copy")) {
      throw new Error("Clipboard unavailable");
    }
  } finally {
    document.body.removeChild(ta);
  }
}
