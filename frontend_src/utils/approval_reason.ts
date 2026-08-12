import { t } from "../i18n";

// Backend-owned reason slugs are data, not operator copy. Keep their labels in
// the catalog and use this helper on every surface that renders a reason.
export const APPROVAL_REASON_LABEL_KEYS: Readonly<Record<string, string>> = {
  token_inactive: "approvals.reason.token_inactive",
  capability_denied: "approvals.reason.capability_denied",
  target_out_of_scope: "approvals.reason.target_out_of_scope",
  target_missing: "approvals.reason.target_missing",
  rate_limited_at_execution: "approvals.reason.rate_limited_at_execution",
  kill_switch: "approvals.reason.kill_switch",
  admin_cancelled: "approvals.reason.admin_cancelled",
  token_revoked: "approvals.reason.token_revoked",
  token_expired: "approvals.reason.token_expired",
  execution_failed: "approvals.reason.execution_failed",
  execution_interrupted: "approvals.reason.execution_interrupted",
  agent_chat_ended: "approvals.reason.agent_chat_ended",
};

/** Localize a backend reason slug; preserve free-form operator/tool text. */
export function localizedApprovalReason(reason: string): string {
  const key = APPROVAL_REASON_LABEL_KEYS[reason];
  return key ? t(key) : reason;
}
