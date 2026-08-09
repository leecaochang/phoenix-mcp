import { t } from "../i18n";

const SOURCE_LABEL_KEYS: Record<string, string> = {
  agentcli: "agentchat.title",
  assist: "settings.assistCard",
  voice: "settings.voiceCard",
  ai_task: "settings.aiTaskCard",
};

/** Present internal request surfaces by their operator-facing product names. */
export function auditSourceLabel(source: string): string {
  const key = SOURCE_LABEL_KEYS[source];
  return key ? t(key) : source;
}

export function isNamedAuditSource(source: string): boolean {
  return source in SOURCE_LABEL_KEYS;
}

/**
 * Convert a unique friendly-name search back to the backend's stored marker.
 * Network addresses and ambiguous text pass through unchanged.
 */
export function auditSourceFilterValue(value: string): string {
  const query = value.trim().toLowerCase();
  if (!query) return value;
  const matches = Object.entries(SOURCE_LABEL_KEYS)
    .filter(([, key]) => t(key).toLowerCase().includes(query));
  return matches.length === 1 ? matches[0][0] : value;
}
