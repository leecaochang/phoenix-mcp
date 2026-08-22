import type { ApprovalDiff } from "../types";
import { hasMessage, t } from "../i18n";

export type ApprovalView = "summary" | "details";

export const APPROVAL_DEFAULT_VIEW_KEY = "phx-approval-default-view";

export function storedApprovalView(): ApprovalView {
  try {
    return localStorage.getItem(APPROVAL_DEFAULT_VIEW_KEY) === "details" ? "details" : "summary";
  } catch {
    return "summary";
  }
}

export function rememberApprovalView(view: ApprovalView): void {
  try {
    localStorage.setItem(APPROVAL_DEFAULT_VIEW_KEY, view);
  } catch {
    // A blocked store makes the choice session-only; Summary remains the next default.
  }
}

export interface FriendlyApprovalSummary {
  title: string;
  body: string;
}

function basename(value: string): string {
  const clean = value.replace(/\\/g, "/").replace(/\/$/, "");
  return clean.slice(clean.lastIndexOf("/") + 1) || value;
}

function targetValue(diff: ApprovalDiff | undefined): string | null {
  const value = diff?.target?.label || diff?.target?.id;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function registryEntityLabel(diff: ApprovalDiff, fallback: string | null): string | null {
  if (fallback && fallback !== diff.target?.id) return fallback;
  const aliases = diff.preview?.aliases;
  if (!aliases || typeof aliases !== "object") return fallback;
  const before = (aliases as Record<string, unknown>).before;
  if (!Array.isArray(before)) return fallback;
  const resolved = before.find((value) =>
    typeof value === "string" && value.trim() && value.trim() !== diff.target?.id,
  );
  return typeof resolved === "string" ? resolved.trim() : fallback;
}

function readableIdentifier(value: string | null): string | null {
  if (!value) return null;
  return /^[a-f0-9]{24,}$/i.test(value) || /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(value)
    ? null
    : value;
}

function removalIntegration(diff: ApprovalDiff): string {
  const owner = diff.preview?.selected_owner;
  if (!owner || typeof owner !== "object") return t("approvalSummary.named.removeDevice.integrationFallback");
  const record = owner as Record<string, unknown>;
  for (const key of ["title", "domain"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return t("approvalSummary.named.removeDevice.integrationFallback");
}

function resourceKind(kind: string, diff: ApprovalDiff): string {
  if (kind !== "helper") return t(`approvalSummary.named.kind.${kind}`);
  const helperType = String(diff.summary_params?.helper_type || "");
  const known = new Set([
    "input_boolean", "input_number", "input_text", "input_select",
    "input_datetime", "input_button", "counter", "timer",
  ]);
  return t(`approvalSummary.named.kind.${known.has(helperType) ? helperType : "helper"}`);
}

function namedResource(key: string, diff: ApprovalDiff, label: string | null): FriendlyApprovalSummary | null {
  const spec: Record<string, { action: "create" | "edit" | "delete"; kind: string }> = {
    create_script: { action: "create", kind: "script" },
    edit_script: { action: "edit", kind: "script" },
    delete_script: { action: "delete", kind: "script" },
    edit_scene: { action: "edit", kind: "scene" },
    delete_scene: { action: "delete", kind: "scene" },
    edit_helper: { action: "edit", kind: "helper" },
    delete_helper: { action: "delete", kind: "helper" },
    delete_automation: { action: "delete", kind: "automation" },
  };
  const resource = spec[key];
  if (!resource) return null;
  const kind = resourceKind(resource.kind, diff);
  if (!label || label === diff.target?.id) {
    if (resource.action !== "delete") return null;
    return {
      title: t("approvalSummary.named.resource.deleteUnnamedTitle", { kind }),
      body: t("approvalSummary.named.resource.deleteUnnamedBody", { kind }),
    };
  }
  const title = t(`approvalSummary.named.resource.${resource.action}Title`, {
    kind, label,
  });
  const body = t(`approvalSummary.named.resource.${resource.action}Body`, {
    kind, label,
  });
  return { title, body };
}

/** Resolve operator copy without interpreting rendered YAML or generating text. */
export function friendlyApprovalSummary(diff: ApprovalDiff | undefined): FriendlyApprovalSummary {
  const summaryKey = diff?.summary_key;
  if (diff && summaryKey?.startsWith("diff.")) {
    const key = summaryKey.slice("diff.".length);
    const label = targetValue(diff);
    const resource = namedResource(key, diff, label);
    if (resource) return resource;
    if (key === "edit_automation" && label) {
      return {
        title: t("approvalSummary.named.editAutomation.title", { label }),
        body: t("approvalSummary.edit_automation.body", diff?.summary_params),
      };
    }
    const registryLabel = key === "set_entity" ? registryEntityLabel(diff, label) : label;
    if ((key === "set_entity" || key === "set_device") && registryLabel) {
      return {
        title: t("approvalSummary.fallback.item.title", { label: registryLabel }),
        body: t(`approvalSummary.${key}.body`, diff?.summary_params),
      };
    }
    if (key === "remove_device") {
      const device = readableIdentifier(label);
      const integration = removalIntegration(diff);
      return device ? {
        title: t("approvalSummary.named.removeDevice.title", { device, integration }),
        body: t("approvalSummary.named.removeDevice.body", { device, integration }),
      } : {
        title: t("approvalSummary.named.removeDevice.unnamedTitle", { integration }),
        body: t("approvalSummary.named.removeDevice.unnamedBody", { integration }),
      };
    }
    const titleKey = `approvalSummary.${key}.title`;
    const bodyKey = `approvalSummary.${key}.body`;
    if (hasMessage(titleKey) && hasMessage(bodyKey)) {
      return {
        title: t(titleKey, diff?.summary_params),
        body: t(bodyKey, diff?.summary_params),
      };
    }
  }

  const target = diff?.target;
  const label = targetValue(diff);
  const type = (target?.type || "").toLowerCase();
  const preview = diff?.preview || {};
  const previewFile = typeof preview.file === "string" ? preview.file : null;
  const file = basename(label || previewFile || "");

  if ((type.includes("file") || type.includes("yaml") || previewFile) && file) {
    return {
      title: t("approvalSummary.fallback.file.title", { file }),
      body: t("approvalSummary.fallback.body"),
    };
  }
  if (type.includes("integration") && label) {
    return {
      title: t("approvalSummary.fallback.integration.title", { label }),
      body: t("approvalSummary.fallback.body"),
    };
  }
  if ((type.includes("device") || type.includes("entity")) && label) {
    return {
      title: t("approvalSummary.fallback.item.title", { label }),
      body: t("approvalSummary.fallback.body"),
    };
  }
  if (type.includes("service") && label) {
    return {
      title: t("approvalSummary.fallback.service.title", { label }),
      body: t("approvalSummary.fallback.body"),
    };
  }
  return {
    title: t("approvalSummary.fallback.unknown.title"),
    body: t("approvalSummary.fallback.body"),
  };
}
