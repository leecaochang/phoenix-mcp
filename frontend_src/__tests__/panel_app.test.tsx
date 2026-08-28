import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, waitFor } from "@testing-library/react";
import { patchDurable as patchAgentCliDurable } from "../utils/agentcli_state";

// Companion to panel_shell.test.tsx. That test stubs react-dom/client to isolate
// the custom-element shell; this one lets the REAL PhoenixApp tree render (with a
// mocked api) so the app-level effects are covered: token loading propagating
// into the list, the pending-approval count badge, and #approvals deep-linking.

vi.mock("../phoenix-mcp-panel.css?inline", () => ({ default: "" }));

const { apiMock } = vi.hoisted(() => {
  const CAPS = [
    "cap_config_read", "cap_camera_read", "cap_template_render", "cap_log_read", "cap_log_control", "cap_search",
    "cap_registry_read", "cap_traces", "cap_diagnostics", "cap_broadcast",
    "cap_service_response", "cap_automation_write", "cap_script_write",
    "cap_scene_write", "cap_helper_write", "cap_physical_control", "cap_restart",
    "cap_integration_write", "cap_integration_reconfigure", "cap_lovelace_write", "cap_registry_write", "cap_radio_write",
    "cap_backup", "cap_filesystem", "cap_yaml_edit",
  ];
  const token = {
    id: "tok-1", name: "shell-token", created_at: new Date().toISOString(),
    created_by: "admin", expires_at: null, revoked: false, last_used_at: null,
    updated_at: null, pass_through: false, announce_all_tools: false,
    confirm_inline_wait_seconds: 0,
    persona: "custom", rate_limit_requests: 60, rate_limit_burst: 10,
    permissions: { domains: {}, devices: {}, entities: {} },
    ...Object.fromEntries(CAPS.map((c) => [c, "deny"])),
  };
  const approval = {
    id: "ap-9", token_id: token.id, token_name: token.name,
    cap_name: "cap_restart", tool_name: "restart_ha", args: {},
    status: "pending", created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    request_id: "req-9", client_ip: "browser",
  };
  const settings = {
    kill_switch: false, disable_all_logging: false, log_allowed: true,
    log_denied: true, log_rate_limited: true, log_entity_names: true,
    log_client_ip: true, notify_on_rate_limit: false, notify_on_approval: true,
    audit_flush_interval: 15, audit_log_maxlen: 10000, mesa_mode: "advisory",
    agentcli_global: true,
  };
  // Specific returns for the app-mount calls; everything else gets a permissive
  // empty shape so any child effect resolves without throwing.
  const overrides: Record<string, (...a: unknown[]) => Promise<unknown>> = {
    listTokens: async () => [token],
    getSettings: async () => settings,
    listApprovals: async () => ({ approvals: [], total: 3 }),
    getApproval: async () => approval,
    getEntityHints: async () => ({ entity_hints: {} }),
  };
  const permissive = {
    approvals: [], tokens: [], items: [], versions: [], total: 0,
    entity_hints: {}, count: 0,
  };
  const apiMock = new Proxy({}, {
    get: (_t, prop: string) => overrides[prop] ?? (async () => permissive),
  });
  return { apiMock };
});

vi.mock("../api", () => ({
  api: apiMock,
  setHass: () => {},
  ApiError: class extends Error {},
}));

await import("../index");

function mountPanel(): HTMLElement & { hass: unknown; narrow: boolean } {
  const el = document.createElement("phoenix-mcp-panel") as HTMLElement & { hass: unknown; narrow: boolean };
  document.body.appendChild(el);
  el.hass = { user: { id: "u1" } };  // render is a no-op until hass is set
  return el;
}

describe("phoenix-mcp-panel full app shell", () => {
  beforeEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
    window.history.replaceState(null, "", "/phoenix-mcp");
  });

  afterEach(() => {
    document.querySelectorAll("phoenix-mcp-panel").forEach((el) => el.remove());
    delete (window as unknown as Record<string, unknown>).__phxAgentChat;
  });

  it("loads tokens through the real tree and shows them in the list", async () => {
    const el = mountPanel();
    await waitFor(() => {
      expect(el.shadowRoot!.textContent).toContain("shell-token");
    });
  });

  it("renders the pending-approval count badge from listApprovals", async () => {
    const el = mountPanel();
    await waitFor(() => {
      const badge = el.shadowRoot!.querySelector(".phx-tab-badge");
      expect(badge?.textContent).toBe("3");
    });
  });

  it("keeps the narrow header and wrapped tabs in one top bar", async () => {
    const el = mountPanel();
    act(() => { el.narrow = true; });
    await waitFor(() => {
      const topbar = el.shadowRoot!.querySelector(".phx-topbar");
      expect(topbar?.children[0]).toHaveClass("phx-header");
      expect(topbar?.children[1]).toHaveClass("phx-tabs");
    });
  });

  it("deep-links to the Approvals tab from the #approvals hash", async () => {
    window.location.hash = "#approvals";
    const el = mountPanel();
    await waitFor(() => {
      // The content panel id tracks the active tab; the hash handler selects it.
      expect(el.shadowRoot!.querySelector("#phx-tabpanel-approvals")).toBeTruthy();
    });
    // Tabs must not reference panel ids that are not mounted (only the active
    // panel exists), so none of them carries aria-controls.
    for (const tab of el.shadowRoot!.querySelectorAll("[role='tab']")) {
      expect(tab.hasAttribute("aria-controls")).toBe(false);
    }
  });

  it("keeps legacy approval hash links opening their specific record", async () => {
    window.location.hash = "#approvals/ap-9";
    const el = mountPanel();
    await waitFor(() => {
      expect(el.shadowRoot!.querySelector("[role='dialog']")).toBeTruthy();
    });
  });

  it("does not re-summon an already-visible global chat when the panel mounts", async () => {
    const open = vi.fn();
    (window as unknown as Record<string, unknown>).__phxAgentChat = {
      ready: true,
      isVisible: () => true,
      open,
    };
    patchAgentCliDurable({ open: true });
    // Home Assistant routes custom-panel subpaths through the panel's route.
    // The Phoenix element mounts only after that navigation has completed.
    window.history.replaceState(null, "", "/phoenix-mcp/approvals/ap-9");

    const el = mountPanel();
    await waitFor(() => {
      expect(el.shadowRoot!.querySelector("[role='dialog']")).toBeTruthy();
    });
    expect(open).not.toHaveBeenCalled();
  });

  it("restores rather than re-centers a hidden global chat after a reload", async () => {
    const open = vi.fn();
    const restore = vi.fn();
    (window as unknown as Record<string, unknown>).__phxAgentChat = {
      ready: true,
      isVisible: () => false,
      open,
      restore,
    };
    patchAgentCliDurable({ open: true, pos: { x: 123, y: 234 } });

    mountPanel();
    await waitFor(() => expect(restore).toHaveBeenCalledOnce());
    expect(open).not.toHaveBeenCalled();
  });
});
