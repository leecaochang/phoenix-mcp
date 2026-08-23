// The panel language preference and its dropdown.
//
// The preference deliberately mirrors the theme one (localStorage, an "auto"
// value that follows Home Assistant): the two injected bundles load on arbitrary
// HA pages outside the panel element and read it from the same origin without
// an API call.

import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

// Enough for the sibling cards on this screen to mount; this test is about the
// language row, and the cards have their own suites.
vi.mock("../api", () => ({
  api: {
    getInfo: () => Promise.resolve({ version: "1.0.0", min_ha_version: "2024.5.0", github_url: "#" }),
    getSettings: () => Promise.resolve({}),
    patchSettings: () => Promise.resolve({}),
    listTokens: () => Promise.resolve([]),
    getAgentCliProviders: () => Promise.resolve({ instances: [] }),
    getAgentCliModels: () => Promise.resolve({ models: [] }),
    getAiTaskPreferred: () => Promise.resolve({ supported: false }),
    getAssistStatus: () => Promise.resolve({ supported: false }),
    getVoiceAgentPipeline: () => Promise.resolve({ supported: false }),
  },
}));

import {
  LANGUAGES,
  LANGUAGE_AUTO,
  getLanguagePreference,
  setLanguagePreference,
  resolveLanguage,
  loadTranslations,
  syncTranslations,
  primeTranslations,
} from "../i18n";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import { SettingsView } from "../views/SettingsView";
import type { GlobalSettings } from "../types";

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("language preference", () => {
  it("defaults to auto when nothing is stored", () => {
    expect(getLanguagePreference()).toBe(LANGUAGE_AUTO);
  });

  it("round-trips a shipped language", () => {
    setLanguagePreference("zh-Hans");
    expect(getLanguagePreference()).toBe("zh-Hans");
  });

  it("falls back to auto for a language this build no longer ships", () => {
    // A locale can be dropped between releases; a stale localStorage value must
    // not pin the panel to a catalog that is not there any more.
    localStorage.setItem("phx-lang", "xx-Fake");
    expect(getLanguagePreference()).toBe(LANGUAGE_AUTO);
  });

  it("auto resolves to the Home Assistant profile language", () => {
    expect(resolveLanguage({ language: "zh-Hans" })).toBe("zh-Hans");
    expect(resolveLanguage({ language: "de" })).toBe("de");
  });

  it("auto falls back to English when hass has no language", () => {
    expect(resolveLanguage(null)).toBe("en");
    expect(resolveLanguage({})).toBe("en");
  });

  it("an explicit choice overrides the profile language", () => {
    setLanguagePreference("en");
    expect(resolveLanguage({ language: "zh-Hans" })).toBe("en");
  });

  it("announces a change so the injected bundles can refetch", () => {
    // Agent Chat and the QuickAdd buttons live outside the panel element and
    // cannot observe this any other way.
    const heard = vi.fn();
    window.addEventListener("phx-language-changed", heard);
    setLanguagePreference("zh-Hans");
    window.removeEventListener("phx-language-changed", heard);
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("survives localStorage being blocked", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(getLanguagePreference()).toBe(LANGUAGE_AUTO);
    expect(() => setLanguagePreference("zh-Hans")).not.toThrow();
    getItem.mockRestore();
    setItem.mockRestore();
  });
});

describe("syncTranslations", () => {
  // These fetch a probe catalog into the module-level store, so put the real
  // one back for the render tests below (setup.ts primes it once per file).
  afterAll(() => primeTranslations(en.panel));

  // Live-found: with the window open, changing the Home Assistant profile
  // language left Agent Chat in the language it opened with, because the
  // catalog was fetched once and nothing re-checked. Under "auto" the answer
  // changes without the panel dropdown ever being touched.
  // The catalog is fetched from Phoenix's own admin API; the requested language
  // is the last path segment, which is what these assert on.
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      json: async () => ({ resources: { [`probe.${url.split("/").pop()}`]: "x" } }),
    }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  const fakeHass = (language: string) => ({
    language,
    auth: { data: { access_token: "tok-test" } },
    fetchWithAuth: (path: string, init?: RequestInit) => fetch(path, init),
  });
  const requestedLanguages = () =>
    fetchMock.mock.calls.map(([url]) => String(url).split("/").pop());

  it("does nothing when the resolved language has not changed", async () => {
    await loadTranslations(fakeHass("en"), "en");
    fetchMock.mockClear();
    expect(await syncTranslations(fakeHass("en"))).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refetches when the profile language changes under auto", async () => {
    await loadTranslations(fakeHass("en"), "en");
    fetchMock.mockClear();
    expect(await syncTranslations(fakeHass("zh-Hans"))).toBe(true);
    expect(requestedLanguages()).toEqual(["zh-Hans"]);
  });

  it("an explicit override wins over the profile language", async () => {
    await loadTranslations(fakeHass("zh-Hans"), "zh-Hans");
    setLanguagePreference("en");
    fetchMock.mockClear();
    expect(await syncTranslations(fakeHass("zh-Hans"))).toBe(true);
    expect(requestedLanguages()).toEqual(["en"]);
  });
});

describe("the dropdown in Settings", () => {
  // The real SettingsView, so a change to the row itself is caught. Rendering a
  // hand-built select here would pass against a component that never shipped one.
  async function renderSettings(language = LANGUAGE_AUTO) {
    const onLanguageChange = vi.fn();
    render(
      <SettingsView
        settings={{} as GlobalSettings}
        onSettingsChange={vi.fn()}
        theme="auto"
        onThemeChange={vi.fn()}
        language={language}
        onLanguageChange={onLanguageChange}
      />,
    );
    const select = (await screen.findByLabelText("Language")) as HTMLSelectElement;
    return { onLanguageChange, select };
  }

  it("offers auto plus every shipped language", async () => {
    const { select } = await renderSettings();
    expect([...select.options].map((o) => o.value)).toEqual([
      LANGUAGE_AUTO,
      ...LANGUAGES.map((l) => l.code),
    ]);
  });

  it("names each language in itself, not in the current UI language", async () => {
    // Someone whose UI is in a language they cannot read still has to be able to
    // find their own, so these labels are endonyms and never catalog entries.
    const { select } = await renderSettings();
    const labels = [...select.options].slice(1).map((o) => o.textContent);
    expect(labels).toEqual(["Deutsch", "English", "Español", "Français", "日本語", "한국어", "Nederlands", "Русский", "中文（简体）", "繁體中文"]);
  });

  it("defaults to Auto", async () => {
    const { select } = await renderSettings();
    expect(select.value).toBe(LANGUAGE_AUTO);
  });

  it("shows the stored choice as selected", async () => {
    const { select } = await renderSettings("zh-Hans");
    expect(select.value).toBe("zh-Hans");
  });

  it("reports the chosen code", async () => {
    const { onLanguageChange, select } = await renderSettings();
    fireEvent.change(select, { target: { value: "zh-Hans" } });
    await waitFor(() => expect(onLanguageChange).toHaveBeenCalledWith("zh-Hans"));
  });

  it("sits next to the theme control", async () => {
    // The user asked for it beside the light/dark toggle; if it drifts into
    // another card this fails rather than quietly moving.
    await renderSettings();
    const row = screen.getByLabelText("Language").closest(".toggle-row");
    expect(row?.parentElement?.textContent).toContain("Theme");
  });
});
