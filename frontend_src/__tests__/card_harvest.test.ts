import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api, currentHass } from "../api";
import { harvestCards, syncCardCatalog } from "../utils/card_harvest";
import { JS_BUILD } from "../version";

// The harvester reads window.customCards, the registry HA's own card picker
// uses, because a card's type strings often do not exist as literals in its
// minified bundle. These tests cover the states that mislead an agent when
// wrong: a card advertised but not defined, a stub that throws, and a harvest
// that saw nothing at all (which must not overwrite a good stored catalog).

vi.mock("../api", () => ({
  api: { postCardCatalog: vi.fn().mockResolvedValue({}) },
  currentHass: vi.fn(),
}));

const LAST_KEY = "phx-card-harvest-at";

// Settling is what the dedicated test below covers; everywhere else it is
// dead wait time, so disable it rather than paying a second per test.
const FAST = { minWaitMs: 0, settleTimeoutMs: 0, followUpMs: [] };

function hass(over: Record<string, unknown> = {}) {
  return {
    user: { is_admin: true },
    auth: { data: { hassUrl: "https://ha.invalid" } },
    states: { "light.kitchen": {}, "sensor.temp": {} },
    connection: { sendMessagePromise: vi.fn().mockResolvedValue([]) },
    ...over,
  };
}

function defineCard(name: string, opts: { stub?: unknown; throws?: boolean; editor?: boolean } = {}) {
  class El extends HTMLElement {
    setConfig() { /* every real card has one */ }
    static getStubConfig() {
      if (opts.throws) throw new Error("needs entities");
      return opts.stub ?? { type: `custom:${name}` };
    }
    static getConfigElement = opts.editor ? () => document.createElement("div") : undefined;
  }
  if (!customElements.get(name)) customElements.define(name, El as unknown as CustomElementConstructor);
}

beforeEach(() => {
  (window as unknown as { customCards?: unknown[] }).customCards = [];
  window.localStorage.clear();
  vi.mocked(currentHass).mockReturnValue(hass());
  vi.mocked(api.postCardCatalog).mockClear();
});

afterEach(() => {
  delete (window as unknown as { customCards?: unknown[] }).customCards;
});

describe("harvestCards", () => {
  it("reads name, description and docs URL from the picker registry", async () => {
    defineCard("harv-basic");
    (window as unknown as { customCards: unknown[] }).customCards = [
      { type: "harv-basic", name: "Basic", description: "Does a thing", documentationURL: "https://x.invalid" },
    ];

    const { entries } = await harvestCards(FAST);

    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      type: "harv-basic",
      name: "Basic",
      description: "Does a thing",
      documentation_url: "https://x.invalid",
      available: true,
      source: "picker",
    });
  });

  it("marks a card advertised without a defined element as unavailable", async () => {
    // Live-observed on a real instance: a picker entry whose element never
    // loaded. Authoring it produces a broken card, so it must not read as usable.
    (window as unknown as { customCards: unknown[] }).customCards = [
      { type: "harv-never-defined", name: "Ghost" },
    ];

    const { entries } = await harvestCards(FAST);

    expect(entries[0].available).toBe(false);
    expect(entries[0].stub_config).toBeNull();
  });

  it("captures a stub config and the visual-editor flag", async () => {
    defineCard("harv-stub", { stub: { type: "custom:harv-stub", entity: "light.kitchen" }, editor: true });
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-stub" }];

    const { entries } = await harvestCards(FAST);

    expect(entries[0].stub_config).toEqual({ type: "custom:harv-stub", entity: "light.kitchen" });
    expect(entries[0].has_visual_editor).toBe(true);
  });

  it("keeps a card whose getStubConfig throws, minus its example", async () => {
    defineCard("harv-throws", { throws: true });
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-throws" }];

    const { entries } = await harvestCards(FAST);

    expect(entries).toHaveLength(1);
    expect(entries[0].stub_config).toBeNull();
  });

  it("de-duplicates repeated registrations", async () => {
    defineCard("harv-dupe");
    (window as unknown as { customCards: unknown[] }).customCards = [
      { type: "harv-dupe", name: "First" },
      { type: "harv-dupe", name: "Second" },
    ];

    const { entries } = await harvestCards(FAST);

    expect(entries).toHaveLength(1);
    expect(entries[0].name).toBe("First");
  });

  it("collects failed imports rather than aborting the harvest", async () => {
    const h = hass({
      connection: {
        sendMessagePromise: vi.fn().mockResolvedValue([
          { type: "module", url: "/hacsfiles/gone/gone.js" },
        ]),
      },
    });
    vi.mocked(currentHass).mockReturnValue(h);
    defineCard("harv-survivor");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-survivor" }];

    const result = await harvestCards(FAST);

    expect(result.failed_imports).toHaveLength(1);
    expect(result.failed_imports[0].url).toBe("/hacsfiles/gone/gone.js");
    expect(result.entries).toHaveLength(1);
    expect(result.resource_count).toBe(1);
  });

  it("returns empty rather than throwing when there is no hass", async () => {
    vi.mocked(currentHass).mockReturnValue(null);

    await expect(harvestCards(FAST)).resolves.toEqual({
      entries: [], resource_count: 0, failed_imports: [],
    });
  });
});

describe("settling before reading the registry", () => {
  // LIVE-FOUND. A harvest taken the instant the panel mounted missed
  // browser_mod's popup-card and browser-mod-tile-card, while a probe minutes
  // earlier had seen both and missed a different card. Registration is
  // asynchronous, so any single instant yields an arbitrary subset. A partial
  // catalog is worse than a slow one: it looks complete, so an agent concludes
  // a card it cannot see is not installed.
  const SETTLE = { minWaitMs: 60, settleTimeoutMs: 2000, settlePollMs: 5, stableSamples: 3, followUpMs: [] };

  it("picks up a card that registers after the imports resolve", async () => {
    defineCard("harv-early");
    defineCard("harv-late");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-early" }];

    // Land the second registration shortly after the harvest begins, the way a
    // late-initialising integration does.
    setTimeout(() => {
      (window as unknown as { customCards: unknown[] }).customCards.push({ type: "harv-late" });
    }, 20);

    const { entries } = await harvestCards(SETTLE);

    expect(entries.map((e) => e.type).sort()).toEqual(["harv-early", "harv-late"]);
  });

  it("gives up at the timeout rather than waiting on a registry that keeps growing", async () => {
    defineCard("harv-grow");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-grow" }];
    // Never stops changing; the timeout is what has to end this.
    const grower = setInterval(() => {
      (window as unknown as { customCards: unknown[] }).customCards.push({ type: "" });
    }, 5);

    try {
      const { entries } = await harvestCards({ minWaitMs: 20, settleTimeoutMs: 120, settlePollMs: 5, stableSamples: 3 });
      expect(entries.some((e) => e.type === "harv-grow")).toBe(true);
    } finally {
      clearInterval(grower);
    }
  });

  it("does not wait out the full timeout on an empty instance", async () => {
    // A genuinely card-free instance must not cost every harvest 5 seconds.
    (window as unknown as { customCards: unknown[] }).customCards = [];
    const started = Date.now();

    await harvestCards({ minWaitMs: 20, settleTimeoutMs: 200, settlePollMs: 5, stableSamples: 3 });

    expect(Date.now() - started).toBeLessThan(1000);
  });
});

describe("syncCardCatalog", () => {
  it("posts a harvest and records the time", async () => {
    defineCard("harv-post");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-post" }];

    await syncCardCatalog(false, FAST);

    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(LAST_KEY)).toBeTruthy();
  });

  it("does nothing for a non-admin", async () => {
    vi.mocked(currentHass).mockReturnValue(hass({ user: { is_admin: false } }));

    await syncCardCatalog(false, FAST);

    expect(api.postCardCatalog).not.toHaveBeenCalled();
  });

  it("skips a re-harvest inside the interval, but force overrides it", async () => {
    defineCard("harv-interval");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-interval" }];
    window.localStorage.setItem(LAST_KEY, `${Date.now()}|${JS_BUILD}`);

    await syncCardCatalog(false, FAST);
    expect(api.postCardCatalog).not.toHaveBeenCalled();

    await syncCardCatalog(true, FAST);
    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
  });

  it("re-harvests after a panel build change even inside the interval", async () => {
    // A new build can harvest differently, so the previous build's timestamp
    // must not suppress it. Otherwise deploying a harvester fix looks like a
    // no-op for the whole interval.
    defineCard("harv-newbuild");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-newbuild" }];
    window.localStorage.setItem(LAST_KEY, `${Date.now()}|0.0.0-old`);

    await syncCardCatalog(false, FAST);

    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(LAST_KEY)).toContain(JS_BUILD);
  });

  it("treats a pre-build-stamp entry as stale rather than crashing on it", async () => {
    defineCard("harv-legacy");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-legacy" }];
    window.localStorage.setItem(LAST_KEY, String(Date.now()));

    await syncCardCatalog(false, FAST);

    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
  });

  it("never posts an empty harvest over a stored catalog", async () => {
    // No resources AND no cards means this page could not see anything, which is
    // far likelier than an instance that genuinely has nothing. Posting it would
    // replace a good catalog with an empty one, and the tool would then tell an
    // agent no custom cards exist.
    (window as unknown as { customCards: unknown[] }).customCards = [];

    await syncCardCatalog(false, FAST);

    expect(api.postCardCatalog).not.toHaveBeenCalled();
  });

  it("re-posts when a card registers after the first report", async () => {
    // The browser_mod shape, which no amount of up-front waiting can cover: its
    // popup-card waits on a custom event, then polls every 1000ms for a global,
    // then awaits its own websocket before registering. Converging beats
    // guessing a delay.
    defineCard("harv-prompt");
    defineCard("harv-slowpoke");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-prompt" }];

    await syncCardCatalog(false, { ...FAST, followUpMs: [30] });
    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.postCardCatalog).mock.calls[0][0]).toMatchObject({
      entries: [{ type: "harv-prompt" }],
    });

    (window as unknown as { customCards: unknown[] }).customCards.push({ type: "harv-slowpoke" });
    await new Promise((r) => setTimeout(r, 90));

    expect(api.postCardCatalog).toHaveBeenCalledTimes(2);
    const second = vi.mocked(api.postCardCatalog).mock.calls[1][0] as { entries: { type: string }[] };
    expect(second.entries.map((e) => e.type).sort()).toEqual(["harv-prompt", "harv-slowpoke"]);
  });

  it("does not re-post when nothing registered late", async () => {
    // A steady instance must cost exactly one write per visit.
    defineCard("harv-steady");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-steady" }];

    await syncCardCatalog(false, { ...FAST, followUpMs: [30] });
    await new Promise((r) => setTimeout(r, 90));

    expect(api.postCardCatalog).toHaveBeenCalledTimes(1);
  });

  it("swallows a post failure", async () => {
    defineCard("harv-postfail");
    (window as unknown as { customCards: unknown[] }).customCards = [{ type: "harv-postfail" }];
    vi.mocked(api.postCardCatalog).mockRejectedValueOnce(new Error("500"));

    await expect(syncCardCatalog(false, FAST)).resolves.toBeUndefined();
    // A failed post must not mark the harvest as done, or the retry is
    // suppressed for the whole interval.
    expect(window.localStorage.getItem(LAST_KEY)).toBeNull();
  });
});
