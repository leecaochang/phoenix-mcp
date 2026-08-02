/** Accessible names under a non-English catalog.
 *
 * accessibility.test.tsx renders against the English catalog that setup.ts
 * primes, so it proves an accessible name EXISTS but never that it is
 * localized. A control whose visible label came from the catalog while its
 * aria-label stayed a hardcoded English literal would pass every check there.
 *
 * This file primes zh-Hans and asserts the accessible names actually change,
 * on the controls where the name is built rather than simply passed through.
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { render, screen } from "@testing-library/react";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import zh from "../../custom_components/phoenix_mcp/catalogs/zh-Hans.json";
import { primeTranslations, flattenCatalog } from "../i18n";
import { PermissionSelector } from "../components/PermissionSelector";
import { AuditTable } from "../components/AuditTable";
import type { AuditEntry } from "../types";

// HA overlays the requested language on English, so a locale missing a key
// still resolves; mirror that here rather than priming zh alone.
beforeAll(() => primeTranslations({ ...flattenCatalog(en.panel), ...flattenCatalog(zh.panel) }));
afterAll(() => primeTranslations(en.panel));

function entry(over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    request_id: "r1",
    timestamp: new Date(0).toISOString(),
    token_id: "t1",
    token_name: "daily",
    method: "POST",
    resource: "call_service",
    outcome: "allowed",
    client_ip: "127.0.0.1",
    ...(over as object),
  } as AuditEntry;
}

describe("accessible names are localized, not just present", () => {
  it("names the permission group and its selected option in Chinese", () => {
    render(
      <PermissionSelector value="GREEN" onChange={() => undefined} label="light.kitchen 的权限" />,
    );
    expect(screen.getByRole("group", { name: "light.kitchen 的权限" })).toBeInTheDocument();
    // Built from perms.selGreen + perms.selSelected, so an untranslated half shows here.
    const chosen = screen.getByRole("button", { name: /读写，已选中/ });
    expect(chosen).toHaveAttribute("aria-pressed", "true");
  });

  it("localizes the audit row's composed accessible name", () => {
    // audit.openEntryAria interpolates a translated outcome label; an English
    // outcome leaking through would be visible in this name.
    render(<AuditTable entries={[entry()]} />);
    const opener = screen.getByRole("button", { name: /已允许/ });
    expect(opener).toBeInTheDocument();
  });

  it("localizes the MESA advisory badge's name and tooltip", () => {
    render(<AuditTable entries={[entry({ mesa_advisory: true })]} />);
    const badge = screen.getByLabelText(/建议模式/);
    expect(badge).toHaveAttribute("title", expect.stringContaining("建议模式"));
  });

  it("leaves no raw catalog keys in any accessible name", () => {
    // A missing key renders as the key itself, which reads as gibberish aloud.
    render(<AuditTable entries={[entry({ mesa_advisory: true })]} />);
    for (const el of screen.getAllByRole("button")) {
      const name = el.getAttribute("aria-label") ?? el.textContent ?? "";
      expect(name).not.toMatch(/^[a-z]+(\.[A-Za-z]+)+$/);
    }
  });
});
