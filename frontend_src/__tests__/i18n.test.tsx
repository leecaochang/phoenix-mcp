import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import {
  flattenCatalog,
  interpolate,
  isI18nReady,
  loadTranslations,
  primeTranslations,
  rawMessage,
  t,
  tn,
} from "../i18n";
import { tRich } from "../i18n/rich";
import { diffSummary } from "../views/ApprovalsView";

// Every test that replaces the catalog must put the real one back, or it leaks
// into the rest of the suite (setup.ts primes it once, globally).
afterEach(() => primeTranslations(en.panel));

describe("flattenCatalog", () => {
  it("collapses nested objects into dotted keys", () => {
    expect(flattenCatalog({ a: { b: "x" }, c: "y" })).toEqual({ "a.b": "x", c: "y" });
  });

  it("ignores non-string leaves and non-objects", () => {
    expect(flattenCatalog({ a: 1, b: null, c: "ok" })).toEqual({ c: "ok" });
    expect(flattenCatalog(null)).toEqual({});
    expect(flattenCatalog("nope")).toEqual({});
  });
});

describe("t", () => {
  it("returns the stored string", () => {
    primeTranslations({ tokens: { revoke: "Revoke" } });
    expect(t("tokens.revoke")).toBe("Revoke");
  });

  it("returns the key itself when it is missing, so gaps are visible", () => {
    primeTranslations({});
    expect(t("tokens.nope")).toBe("tokens.nope");
  });

  it("substitutes {name} params", () => {
    primeTranslations({ audit: { open: "Open audit entry {outcome} for {name}" } });
    expect(t("audit.open", { outcome: "Denied", name: "kitchen" })).toBe(
      "Open audit entry Denied for kitchen",
    );
  });

  it("accepts numbers and leaves an unsupplied param literal", () => {
    primeTranslations({ x: { y: "{count} of {total}" } });
    expect(t("x.y", { count: 3 })).toBe("3 of {total}");
  });

  it("preserves leading and trailing whitespace exactly", () => {
    // Several catalog strings are sentence fragments whose spacing is what
    // joins them to their neighbour; losing it silently changes the rendering.
    primeTranslations({ mesa: { prefix: "Current filter: " } });
    expect(t("mesa.prefix")).toBe("Current filter: ");
  });
});

describe("tn", () => {
  it("picks one for a count of exactly 1 and other for anything else", () => {
    primeTranslations({
      selectBy: { entities: { one: "{count} entity", other: "{count} entities" } },
    });
    expect(tn("selectBy.entities", 1)).toBe("1 entity");
    expect(tn("selectBy.entities", 0)).toBe("0 entities");
    expect(tn("selectBy.entities", 7)).toBe("7 entities");
  });

  it("passes extra params through alongside count", () => {
    primeTranslations({ p: { one: "{count} {noun}", other: "{count} {noun}s" } });
    expect(tn("p", 2, { noun: "profile" })).toBe("2 profiles");
  });
});

describe("tRich", () => {
  it("renders tagged segments through the supplied renderers", () => {
    primeTranslations({ w: { s: "Select <strong>W</strong> to grant access." } });
    render(
      <p data-testid="out">
        {tRich("w.s", { strong: (c) => <strong>{c}</strong> })}
      </p>,
    );
    expect(screen.getByTestId("out")).toHaveTextContent("Select W to grant access.");
    expect(screen.getByText("W").tagName).toBe("STRONG");
  });

  it("keeps whitespace inside a tag, which some sentences depend on", () => {
    primeTranslations({ w: { s: "Select<strong> W</strong> to grant." } });
    render(<p>{tRich("w.s", { strong: (c) => <strong>{c}</strong> })}</p>);
    expect(screen.getByText("W", { exact: false }).textContent).toBe(" W");
  });

  it("interpolates params inside and outside tags", () => {
    primeTranslations({ w: { s: "Token <code>{name}</code> is {state}." } });
    render(
      <p data-testid="out">
        {tRich("w.s", { code: (c) => <code>{c}</code> }, { name: "daily", state: "ready" })}
      </p>,
    );
    expect(screen.getByTestId("out")).toHaveTextContent("Token daily is ready.");
    expect(screen.getByText("daily").tagName).toBe("CODE");
  });

  it("never lets a param value introduce markup", () => {
    // Tags are parsed from the catalog string before params are substituted.
    primeTranslations({ w: { s: "Hello {who}" } });
    render(
      <p data-testid="out">{tRich("w.s", { strong: (c) => <strong>{c}</strong> }, { who: "<strong>x</strong>" })}</p>,
    );
    expect(screen.getByTestId("out").querySelector("strong")).toBeNull();
    expect(screen.getByTestId("out")).toHaveTextContent("Hello <strong>x</strong>");
  });

  it("renders multiple tags in one sentence", () => {
    primeTranslations({ w: { s: "Expand <code>light</code> then press <strong>Send</strong>." } });
    render(
      <p data-testid="out">
        {tRich("w.s", { code: (c) => <code>{c}</code>, strong: (c) => <strong>{c}</strong> })}
      </p>,
    );
    expect(screen.getByText("light").tagName).toBe("CODE");
    expect(screen.getByText("Send").tagName).toBe("STRONG");
  });

  it("leaves an unrendered tag visible rather than dropping its words", () => {
    primeTranslations({ w: { s: "A <em>b</em> c" } });
    render(<p data-testid="out">{tRich("w.s", {})}</p>);
    expect(screen.getByTestId("out")).toHaveTextContent("A <em>b</em> c");
  });
});

describe("loadTranslations", () => {
  // The catalog comes from Phoenix's own admin API, not HA's translation
  // websocket: these strings cannot live in translations/ because hassfest
  // rejects any non-HA category there, which fails the HACS submission.
  const mockFetch = (body: unknown, ok = true) => {
    const fn = vi.fn().mockResolvedValue({ ok, json: async () => body });
    vi.stubGlobal("fetch", fn);
    return fn;
  };
  const authenticatedHass = (token = "tok-test") => ({
    auth: { data: { access_token: token } },
  });

  afterEach(() => vi.unstubAllGlobals());

  it("requests the catalog for the language, with the admin bearer token", async () => {
    // The panel's only wire contract with the backend. Nothing else in the
    // suite covers it, because setup.ts primes the catalog rather than fetching.
    const fetchMock = mockFetch({ resources: {} });
    await loadTranslations({ auth: { data: { access_token: "tok-123" } } }, "zh-Hans");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/catalog/zh-Hans",
      { headers: { Authorization: "Bearer tok-123" } },
    );
  });

  it("installs the served resources, which are already dotted and unprefixed", async () => {
    mockFetch({ resources: { "tokens.revoke": "Revoke" } });
    await loadTranslations(authenticatedHass(), "en");
    expect(t("tokens.revoke")).toBe("Revoke");
    expect(isI18nReady()).toBe(true);
  });

  it("ignores a non-string value rather than rendering it", async () => {
    primeTranslations({ tokens: { revoke: "Revoke" } });
    mockFetch({ resources: { "tokens.revoke": "撤销", "tokens.bad": { nested: 1 } } });
    await loadTranslations(authenticatedHass(), "zh-Hans");
    expect(t("tokens.revoke")).toBe("撤销");
    expect(t("tokens.bad")).toBe("tokens.bad");
  });

  it("keeps the existing catalog when a fetch returns nothing", async () => {
    primeTranslations({ tokens: { revoke: "Revoke" } });
    mockFetch({ resources: {} });
    await loadTranslations(authenticatedHass(), "en");
    expect(t("tokens.revoke")).toBe("Revoke");
  });

  it("refreshes the token once and retries on a 401", async () => {
    // An expired access token is otherwise a panel full of raw keys until the
    // next full page load, plus a ban-log warning from HA.
    const refreshAccessToken = vi.fn(async () => {
      hass.auth.data.access_token = "fresh";
    });
    const hass = { auth: { data: { access_token: "stale" }, refreshAccessToken } };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ status: 401, ok: false, json: async () => null })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ resources: { a: "A" } }) });
    vi.stubGlobal("fetch", fetchMock);

    await loadTranslations(hass, "en");

    expect(refreshAccessToken).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[1][1]).toEqual({
      headers: { Authorization: "Bearer fresh" },
    });
    expect(t("a")).toBe("A");
  });

  it("refreshes an expiring token before the first request", async () => {
    const hass = {
      auth: {
        data: { access_token: "stale", expires: Date.now() + 10_000 },
        refreshAccessToken: vi.fn(async () => {
          hass.auth.data.access_token = "fresh";
          hass.auth.data.expires = Date.now() + 300_000;
        }),
      },
    };
    const fetchMock = mockFetch({ resources: { a: "A" } });

    await loadTranslations(hass, "en");

    expect(hass.auth.refreshAccessToken).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/catalog/en",
      { headers: { Authorization: "Bearer fresh" } },
    );
  });

  it("uses the current Home Assistant root when the captured hass is unavailable", async () => {
    const homeAssistant = document.createElement("home-assistant") as HTMLElement & {
      hass: ReturnType<typeof authenticatedHass>;
    };
    homeAssistant.hass = authenticatedHass("live-token");
    document.body.appendChild(homeAssistant);
    const fetchMock = mockFetch({ resources: { a: "A" } });

    await loadTranslations(null, "en");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/phoenix-mcp/admin/catalog/en",
      { headers: { Authorization: "Bearer live-token" } },
    );
    homeAssistant.remove();
  });

  it("does not probe the admin catalog when no access token is available", async () => {
    const fetchMock = mockFetch({ resources: { a: "A" } });
    await loadTranslations({}, "en");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(isI18nReady()).toBe(true);
  });

  it("keeps the existing catalog when the request fails", async () => {
    primeTranslations({ tokens: { revoke: "Revoke" } });
    mockFetch(null, false);
    await loadTranslations(authenticatedHass(), "en");
    expect(t("tokens.revoke")).toBe("Revoke");
    expect(isI18nReady()).toBe(true);
  });

  it("survives a rejected request and still reports ready", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await loadTranslations(authenticatedHass(), "en");
    expect(isI18nReady()).toBe(true);
  });
});

describe("catalog hygiene", () => {
  it("has no stray braces that HA's placeholder validation would choke on", () => {
    // HA parses every string with Python's string.Formatter to compare
    // placeholders against English. A lone brace raises there, so it must never
    // reach the file.
    for (const [key, value] of Object.entries(flattenCatalog(en.panel))) {
      const withoutParams = value.replace(/\{\w+\}/g, "");
      expect(withoutParams, `stray brace in ${key}`).not.toMatch(/[{}]/);
    }
  });

  it("uses no em dashes, en dashes, or arrows", () => {
    for (const [key, value] of Object.entries(flattenCatalog(en.panel))) {
      expect(value, `disallowed character in ${key}`).not.toMatch(/[—–→]/);
    }
  });
});

describe("rawMessage and interpolate", () => {
  it("rawMessage returns the uninterpolated template", () => {
    primeTranslations({ a: "hi {name}" });
    expect(rawMessage("a")).toBe("hi {name}");
  });

  it("interpolate is a no-op without params", () => {
    expect(interpolate("hi {name}")).toBe("hi {name}");
  });
});

describe("diffSummary", () => {
  const rec = (diff: unknown) => diff as never;

  it("renders the keyed summary in the operator's language", () => {
    primeTranslations({ diff: { "edit_automation": "Bearbeite Automatisierung '{automation_id}'" } });
    expect(diffSummary(rec({
      summary: "Edit automation 'porch'",
      summary_key: "diff.edit_automation",
      summary_params: { automation_id: "porch" },
    }))).toBe("Bearbeite Automatisierung 'porch'");
  });

  it("falls back to the stored English for a pre-upgrade record", () => {
    // Records written before summary_key existed carry only the sentence.
    expect(diffSummary(rec({ summary: "Restart Home Assistant" })))
      .toBe("Restart Home Assistant");
  });

  it("falls back when the bundle does not know the key", () => {
    // A newer backend behind a cached panel: never show a raw key on the
    // surface an admin approves from.
    primeTranslations({});
    expect(diffSummary(rec({
      summary: "Flash ESPHome firmware to attic",
      summary_key: "diff.some_future_tool",
      summary_params: { label: "attic" },
    }))).toBe("Flash ESPHome firmware to attic");
  });

  it("is empty rather than undefined when there is no diff at all", () => {
    expect(diffSummary(undefined)).toBe("");
  });
});
