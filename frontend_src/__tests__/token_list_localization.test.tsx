/** Localized token status filtering and sorting. */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import de from "../../custom_components/phoenix_mcp/catalogs/de.json";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import { flattenCatalog, primeTranslations, setFormatLocale } from "../i18n";
import type { TokenRecord } from "../types";
import { TokenListView } from "../views/TokenList";

beforeAll(() => {
  primeTranslations({ ...flattenCatalog(en.panel), ...flattenCatalog(de.panel) });
  setFormatLocale({ language: "de" }, "de");
});

afterAll(() => {
  primeTranslations(en.panel);
  setFormatLocale({ language: "en" }, "en");
});

function token(overrides: Partial<TokenRecord>): TokenRecord {
  return {
    id: "active",
    name: "Active token",
    created_at: "2026-01-01T00:00:00Z",
    created_by: "admin",
    expires_at: null,
    revoked: false,
    last_used_at: null,
    updated_at: null,
    pass_through: false,
    persona: "custom",
    confirm_inline_wait_seconds: 0,
    rate_limit_requests: 60,
    rate_limit_burst: 10,
    cap_automation_write: "deny",
    cap_script_write: "deny",
    cap_blueprint_write: "deny",
    cap_log_read: "deny",
    cap_log_control: "deny",
    cap_config_read: "deny",
    cap_template_render: "deny",
    cap_restart: "deny",
    cap_physical_control: "deny",
    cap_service_response: "deny",
    cap_broadcast: "deny",
    cap_search: "deny",
    cap_registry_read: "deny",
    cap_traces: "deny",
    cap_diagnostics: "deny",
    cap_scene_write: "deny",
    cap_helper_write: "deny",
    cap_integration_write: "deny",
    cap_integration_reconfigure: "deny",
    cap_lovelace_write: "deny",
    cap_registry_write: "deny",
    cap_radio_write: "deny",
    cap_energy_write: "deny",
    cap_backup: "deny",
    cap_filesystem: "deny",
    cap_yaml_edit: "deny",
    cap_esphome_yaml: "deny",
    cap_esphome_flash: "deny",
    cap_camera_read: "deny",
    permissions: { domains: {}, devices: {}, entities: {} },
    presets: [],
    active_preset_id: null,
    ...overrides,
  };
}

const callbacks = {
  onRefresh: vi.fn(),
  onOpenDetail: vi.fn(),
  onLaunchWizard: vi.fn(),
  onOpenCreate: vi.fn(),
  onCloseCreate: vi.fn(),
  onOpenSettings: vi.fn(),
};

function renderList(tokens: TokenRecord[]) {
  return render(
    <TokenListView
      tokens={tokens}
      loading={false}
      error={null}
      showCreate={false}
      {...callbacks}
    />,
  );
}

describe("localized token statuses", () => {
  it("filters by the same localized label the table displays", () => {
    renderList([
      token({ id: "active", name: "Daily agent" }),
      token({ id: "expired", name: "Old agent", expires_at: "2020-01-01T00:00:00Z" }),
    ]);

    fireEvent.change(screen.getByRole("textbox", { name: de.panel.tokens.filterAria }), {
      target: { value: de.panel.tokens.statusActive },
    });

    expect(screen.getByText("Daily agent")).toBeInTheDocument();
    expect(screen.queryByText("Old agent")).not.toBeInTheDocument();
  });

  it("sorts status rows by their localized display labels", () => {
    renderList([
      token({ id: "active", name: "Active row" }),
      token({ id: "expired", name: "Expired row", expires_at: "2020-01-01T00:00:00Z" }),
      token({ id: "revoked", name: "Revoked row", revoked: true }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: /Status/ }));
    const names = screen.getAllByRole("row").slice(1).map(
      (row) => row.querySelector(".token-name")?.textContent,
    );
    expect(names).toEqual(["Expired row", "Active row", "Revoked row"]);
  });
});
