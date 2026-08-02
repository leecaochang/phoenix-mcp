/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Dashboard card catalog harvester.
 *
 * Builds the list of custom Lovelace cards this instance can actually render and
 * posts it to the backend, so the MCP tool surface can answer "what cards do I
 * have" before an agent authors one.
 *
 * WHY THIS RUNS IN THE BROWSER. A card announces itself by pushing onto
 * `window.customCards`, the registry HA's own card picker reads, and it does so
 * at runtime. Roughly half of a real plugin set builds its type strings by
 * concatenation, so those strings never appear as literals in the shipped
 * bundle: Mushroom assembles all 18 of its types from `"mushroom"` plus a
 * suffix. Measured on a 24-plugin instance, parsing the files on disk found 16
 * types with no descriptions and no examples, while this found 65 cards, most
 * with a worked example config. The backend genuinely cannot do this.
 *
 * Design rules, matching the MESA injector next door:
 *  - Admin only, and every entry point is wrapped so a throw logs and no-ops.
 *    A harvest failure must never break the panel or an HA page.
 *  - Best effort per card: one card whose getStubConfig throws costs that card
 *    its example, never the whole catalog.
 *  - Idempotent and cheap to repeat. `import()` is deduplicated by URL in the
 *    browser, so on an instance where HA already loaded the resources the import
 *    loop is a no-op; on one that starts cold it does the real work.
 */

import { api, currentHass } from "../api";
import { JS_BUILD } from "../version";

const LOG = "[Phoenix MCP cards]";

// Skip a re-harvest within this window. The catalog only changes when the
// operator installs or removes a plugin, so re-posting on every panel mount
// during a working session is pure noise.
const MIN_INTERVAL_MS = 10 * 60 * 1000;
const LAST_KEY = "phx-card-harvest-at";

export interface HarvestedCard {
  type: string;
  name: string | null;
  description: string | null;
  documentation_url: string | null;
  preview: boolean;
  available: boolean;
  has_visual_editor: boolean;
  stub_config: unknown;
  source: "picker" | "element";
}

export interface HarvestResult {
  entries: HarvestedCard[];
  resource_count: number;
  failed_imports: { url: string; error: string }[];
}

function log(...args: unknown[]): void {
  // eslint-disable-next-line no-console
  console.debug(LOG, ...args);
}

/** Read the registered Lovelace resources. Falls back to the pre-2025.1 command name. */
async function readResources(hass: any): Promise<any[]> {
  try {
    return (await hass.connection.sendMessagePromise({ type: "lovelace/resources/list" })) ?? [];
  } catch {
    // Storage mode registers only `/list`; YAML mode also kept the bare name for
    // back-compat. Trying both means neither mode is a special case here.
    try {
      return (await hass.connection.sendMessagePromise({ type: "lovelace/resources" })) ?? [];
    } catch (err) {
      log("could not read the resource list", err);
      return [];
    }
  }
}

/**
 * Import every module resource, mirroring HA's own loadLovelaceResources.
 *
 * Only `module` is handled: `js` and `css` resources cannot register a card
 * element the modern way, and HA itself treats them as separate paths. A failed
 * import is collected rather than thrown, because a registered resource whose
 * file is gone is a real and common condition (a live instance carried four,
 * two of them stale duplicates) and it must not stop the remaining imports.
 */
async function loadModules(hass: any, resources: any[]): Promise<{ url: string; error: string }[]> {
  const base = hass?.auth?.data?.hassUrl || window.location.origin;
  const failed: { url: string; error: string }[] = [];
  for (const r of resources) {
    if (!r || r.type !== "module" || typeof r.url !== "string") continue;
    let url: string;
    try {
      url = new URL(r.url, base).toString();
    } catch {
      failed.push({ url: String(r.url), error: "unparseable url" });
      continue;
    }
    try {
      await import(/* @vite-ignore */ url);
    } catch (err) {
      failed.push({ url: r.url, error: String(err).slice(0, 200) });
    }
  }
  return failed;
}

function cleanText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function registry(): any[] {
  const r = (window as any).customCards;
  return Array.isArray(r) ? r : [];
}

/**
 * LIVE-FOUND, and the reason the settle and follow-up machinery below exists:
 * cards do not all register by the time the imports resolve. A harvest taken the
 * instant the panel mounted missed browser_mod's popup-card and
 * browser-mod-tile-card while catching a card a slightly earlier probe had
 * missed, and saw tiktoktts-card's element as defined where the earlier probe
 * had not. Registration is asynchronous, so any single instant yields an
 * arbitrary subset.
 *
 * A partial catalog is the dangerous failure here, worse than a slow one: the
 * response looks complete, so an agent concludes a card it cannot see is not
 * installed.
 */
export interface HarvestOptions {
  /**
   * Always wait at least this long before reading the registry. 0 disables
   * settling entirely (tests).
   */
  minWaitMs?: number;
  /** Stop waiting for a still-growing registry after this long. */
  settleTimeoutMs?: number;
  settlePollMs?: number;
  /** Consecutive unchanged reads that count as settled, past minWaitMs. */
  stableSamples?: number;
  /**
   * Delays after the first post at which to re-check for cards that registered
   * late, re-posting only if the set grew. Empty disables follow-ups (tests).
   */
  followUpMs?: number[];
}

const SETTLE_DEFAULTS = {
  minWaitMs: 3000,
  settleTimeoutMs: 10000,
  settlePollMs: 250,
  stableSamples: 4,
  // 15s covers a plugin waiting on its own websocket; 60s covers one that had a
  // slow connection or a retry. Past that, the next panel visit picks it up.
  followUpMs: [15000, 60000],
};

/**
 * Wait for window.customCards to stop growing before reading it.
 *
 * A MINIMUM WAIT IS THE LOAD-BEARING PART, and stability alone is not enough.
 * Registrations do not arrive as one burst: an integration-provided card can
 * land seconds after everything else has gone quiet, and a purely
 * stability-based wait returns during that quiet gap and misses it. So this
 * always waits minWaitMs, and only then treats a steady count as settled.
 *
 * This is a mitigation, not a guarantee: a card registering after the window is
 * simply picked up by the next harvest. It is sized so a normal instance is
 * consistently complete, because the catalog is REPLACED wholesale on each
 * report (so an uninstalled card disappears rather than being recommended
 * forever), which means an unusually incomplete harvest would briefly shrink
 * the catalog. Consistency matters more here than speed: this runs in the
 * background, at most once every ten minutes, and blocks nothing.
 */
async function settleRegistry(opts: HarvestOptions): Promise<void> {
  const { minWaitMs, settleTimeoutMs, settlePollMs, stableSamples } = { ...SETTLE_DEFAULTS, ...opts };
  if (minWaitMs <= 0 && settleTimeoutMs <= 0) return;
  const started = Date.now();
  const deadline = started + Math.max(minWaitMs, settleTimeoutMs);
  let last = -1;
  let stable = 0;
  while (Date.now() < deadline) {
    const count = registry().length;
    stable = count === last ? stable + 1 : 0;
    if (Date.now() - started >= minWaitMs && stable >= stableSamples) return;
    last = count;
    await new Promise((r) => window.setTimeout(r, settlePollMs));
  }
  log(`registry still changing after ${Date.now() - started}ms; harvesting ${registry().length} cards anyway`);
}

/**
 * Ask one card element for a starting config.
 *
 * This is what HA's card picker calls to seed a new card, so it returns
 * something the card itself considers valid, which is the closest thing to a
 * config schema that custom cards publish. It may be sync or async and it may
 * throw on a card that expects entities it cannot find; either way the card
 * still belongs in the catalog, just without an example.
 */
async function stubFor(el: any, hass: any): Promise<unknown> {
  if (typeof el?.getStubConfig !== "function") return null;
  const ids = Object.keys(hass?.states ?? {});
  try {
    return (await el.getStubConfig(hass, ids.slice(0, 40), ids.slice(40, 80))) ?? null;
  } catch {
    return null;
  }
}

/** Candidate element names for cards that deliberately skip the picker. */
function candidateNames(resources: any[], known: Set<string>): string[] {
  const out = new Set<string>();
  for (const r of resources) {
    if (typeof r?.url !== "string") continue;
    const path = r.url.split("?")[0];
    const stem = (path.split("/").pop() || "").replace(/\.js$/, "");
    const dir = path.split("/").slice(-2)[0] || "";
    for (const n of [stem, stem.replace(/\.min$/, ""), stem.replace(/-bundle$/, ""), dir]) {
      // A custom element name must contain a hyphen, which also filters out
      // most non-card filenames for free.
      if (n && n.includes("-") && !known.has(n)) out.add(n);
    }
  }
  return [...out];
}

/** Build the catalog from what this page can see. Never throws. */
export async function harvestCards(opts: HarvestOptions = {}): Promise<HarvestResult> {
  const hass = currentHass() as any;
  if (!hass) return { entries: [], resource_count: 0, failed_imports: [] };

  const resources = await readResources(hass);
  const failed = await loadModules(hass, resources);
  // Imports resolving is NOT the same as every card having registered; see
  // settleRegistry for the live evidence.
  await settleRegistry(opts);

  const entries: HarvestedCard[] = [];
  const seen = new Set<string>();

  for (const c of registry()) {
    const type = cleanText(c?.type);
    if (!type || seen.has(type)) continue;
    seen.add(type);
    const el = customElements.get(type) as any;
    entries.push({
      type,
      name: cleanText(c?.name),
      description: cleanText(c?.description),
      documentation_url: cleanText(c?.documentationURL),
      preview: !!c?.preview,
      // A card can advertise itself and never define its element (live-observed
      // on a real instance). Recording that as unavailable is the whole reason
      // the field exists: the agent must not author a card that cannot render.
      available: !!el,
      has_visual_editor: typeof el?.getConfigElement === "function",
      stub_config: el ? await stubFor(el, hass) : null,
      source: "picker",
    });
  }

  // Cards that register an element but skip the picker on purpose so they do not
  // clutter it (stack-in-card, config-template-card and friends). They are real
  // and widely used, so a catalog listing only picker cards would tell an agent
  // they do not exist. Probing by name is a heuristic and is marked as such via
  // `source`, since there is no way to enumerate the element registry.
  for (const name of candidateNames(resources, seen)) {
    const el = customElements.get(name) as any;
    if (!el || typeof el.prototype?.setConfig !== "function") continue;
    entries.push({
      type: name,
      name: null,
      description: null,
      documentation_url: null,
      preview: false,
      available: true,
      has_visual_editor: typeof el.getConfigElement === "function",
      stub_config: await stubFor(el, hass),
      source: "element",
    });
  }

  return { entries, resource_count: resources.length, failed_imports: failed };
}

function harvestedRecently(): boolean {
  try {
    const raw = window.localStorage.getItem(LAST_KEY);
    if (!raw) return false;
    const [at, build] = raw.split("|");
    // A new panel build may harvest DIFFERENTLY (this module has already gained
    // a settle wait that changed what it captures), so the previous run's
    // timestamp must not suppress the first harvest of a new build. Without
    // this, deploying a harvester fix and reloading appears to do nothing for
    // up to the full interval, which reads as the fix not working.
    if (build !== JS_BUILD) return false;
    const ts = Number(at);
    return Number.isFinite(ts) && Date.now() - ts < MIN_INTERVAL_MS;
  } catch {
    return false;
  }
}

function markHarvested(): void {
  try {
    window.localStorage.setItem(LAST_KEY, `${Date.now()}|${JS_BUILD}`);
  } catch {
    /* private browsing; re-harvesting more often is harmless */
  }
}

/**
 * Harvest and post, unless an admin did so recently. Never throws.
 *
 * `force` skips the interval check, for an explicit operator-triggered rescan.
 */
export async function syncCardCatalog(force = false, opts: HarvestOptions = {}): Promise<void> {
  try {
    const hass = currentHass() as any;
    if (!hass?.user?.is_admin) return;
    if (!force && harvestedRecently()) return;
    const result = await harvestCards(opts);
    // A harvest that saw no resources at all is more likely a page that could
    // not reach the WS connection than an instance with nothing installed.
    // Posting it would overwrite a good catalog with an empty one.
    if (result.resource_count === 0 && result.entries.length === 0) {
      log("nothing to report; leaving the stored catalog alone");
      return;
    }
    await api.postCardCatalog(result);
    markHarvested();
    log(`reported ${result.entries.length} cards from ${result.resource_count} resources`);
    scheduleFollowUps(result.entries.length, opts);
  } catch (err) {
    log("harvest failed", err);
  }
}

/**
 * Re-check later for cards that had not registered yet, re-posting if the set grew.
 *
 * WHY THIS EXISTS RATHER THAN A LONGER WAIT. There is no signal for "every card
 * has registered", and no fixed delay can be correct, because each plugin
 * invents its own readiness. browser_mod is the worked example: its popup-card
 * waits on a custom event, then polls every 1000ms for a global, then awaits its
 * own websocket connection before registering. Nothing in Home Assistant knows
 * that has happened, and no amount of waiting up front is guaranteed to cover it.
 *
 * So rather than trying to pick the right instant, converge on it. Registrations
 * only ever ADD within a page session, so a later harvest from the same page is
 * a superset of an earlier one, and re-posting is safe against the backend's
 * wholesale replacement precisely because of that. Posting only on growth keeps
 * a steady instance to exactly one write per visit.
 */
function scheduleFollowUps(bestSoFar: number, opts: HarvestOptions): void {
  const delays = opts.followUpMs ?? SETTLE_DEFAULTS.followUpMs;
  let best = bestSoFar;
  for (const delay of delays) {
    window.setTimeout(() => {
      void (async () => {
        try {
          // No settle wait: we are already well past page load, so read now.
          const later = await harvestCards({ ...opts, minWaitMs: 0, settleTimeoutMs: 0 });
          if (later.entries.length <= best) return;
          await api.postCardCatalog(later);
          log(`re-reported ${later.entries.length} cards (was ${best}; ${later.entries.length - best} registered late)`);
          best = later.entries.length;
          markHarvested();
        } catch (err) {
          log("follow-up harvest failed", err);
        }
      })();
    }, delay);
  }
}
