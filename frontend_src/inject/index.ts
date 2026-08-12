/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * In-context MESA profile injector.
 *
 * Loaded on every HA page via the frontend extra-module mechanism, but only when
 * the admin enabled `mesa_inject_enabled` (the Python side registers this module
 * conditionally). It adds a compact "+" / "✓" control (create / already profiled)
 * so an admin can author a MESA profile in place, on two kinds of surface:
 *
 *  - Entity scope: one control per ROW of the data-table config pages listed in
 *    `ENTITY_PAGE_PREFIXES` (./dom), covering Entities, Automations, Scripts,
 *    Scenes, Helpers and People. Entities is the broad one, since every entity is
 *    listed there; the per-domain pages exist for in-context convenience.
 *  - Device, area and integration scope: one control in a detail page's HEADER,
 *    keyed from the URL rather than from a DOM id, which makes these three far
 *    less fragile than the row adapter. They share one scanner, including its
 *    stale-key check: navigating between two pages of the same kind reuses the
 *    DOM, so a leftover button would edit the previously viewed target.
 *
 * Domain scope has no injected surface, because Home Assistant has no per-domain
 * page for a control to live on; domain profiles are authored in the panel's
 * MESA tab. The scope set here derives from the modal's own union rather than
 * being restated, so the two cannot disagree.
 *
 * Design rules:
 *  - Admin only: does nothing unless `hass.user.is_admin`.
 *  - Fully sandboxed: every entry point is wrapped so a thrown error logs and
 *    no-ops; it must never break the HA frontend.
 *  - Single current-HA path with feature-detection: if the data-table DOM does
 *    not match, nothing is injected (the feature self-disables). This is the
 *    intended behaviour on older HA or after a breaking HA frontend release.
 *  - The heavy modal (React + ProfileEditor) is lazy-imported on first click.
 *
 * THE ONE FRAGILE SPOT is `extractEntityId()` in ./dom: it reads each row's id
 * from HA's ha-data-table. If a future HA release changes that, update only that
 * function (and bump MESA_INJECT_MIN_HA in const.py if needed).
 */

import { api, setHass } from "../api";
import { JS_BUILD } from "../version";
import { syncCardCatalog } from "../utils/card_harvest";
import { loadTranslations, resolveLanguage, syncTranslations, t } from "../i18n";
import { BTN_CLASS, deepQueryAll, extractEntityId, isSelfMutation, nameInsertionPoint, onEntityPage, SUBPAGE_SURFACES, WIDEN_STYLE_ID } from "./dom";
import { claimInjectController, type InjectController } from "./ownership";
import type { QuickAddScope } from "./QuickAdd";

const LOG = "[Phoenix MCP inject]";
const POLL_MS = 2000;
const DEBOUNCE_MS = 150;

// The scopes this injector decorates, taken from the modal it opens rather
// than restated, so a level the modal accepts can never be unreachable here.
type Scope = QuickAddScope;

// One set per scope, keyed by the union: a scope added without its set is a
// build error rather than a button that never lights up. Each holds the keys
// that already have a stored profile, which is what picks the glyph.
const profiled: Record<Scope, Set<string>> = {
  entity: new Set(),
  device: new Set(),
  area: new Set(),
  integration: new Set(),
};
let observedRoots = new WeakSet<ShadowRoot>();
const observers = new Set<MutationObserver>();
const timeouts = new Set<number>();
const intervals = new Set<number>();
const listeners: Array<{ type: string; handler: EventListener }> = [];
let disposed = false;
let debounceTimer: number | undefined;

function trackedTimeout(fn: () => void, delay: number): number {
  const id = window.setTimeout(() => {
    timeouts.delete(id);
    if (!disposed) fn();
  }, delay);
  timeouts.add(id);
  return id;
}

function trackedInterval(fn: () => void, delay: number): number {
  const id = window.setInterval(() => { if (!disposed) fn(); }, delay);
  intervals.add(id);
  return id;
}

function log(...args: unknown[]): void {
  // Quiet by default; visible with verbose console logging.
  // eslint-disable-next-line no-console
  console.debug(LOG, ...args);
}

// Opt-in diagnostics: localStorage["phx-inject-debug"] = "1" then reload.
// Logs injector state changes for duplicate-button and profile-state debugging.
const DEBUG: boolean = (() => {
  try {
    return localStorage.getItem("phx-inject-debug") === "1";
  } catch {
    return false;
  }
})();
function dbg(...args: unknown[]): void {
  // eslint-disable-next-line no-console
  if (DEBUG) console.info(LOG, ...args);
}

function getHass(): any {
  return (document.querySelector("home-assistant") as any)?.hass ?? null;
}

/** Where a button is going, which is the only thing that changes about it.
 *
 *  "cell" is a data-table row, where it shares a narrow icon column with the
 *  entity icon. "toolbar" is a detail page's action row, where it sits beside
 *  Home Assistant's own 48px icon buttons and a 26px control reads as debris.
 */
type Placement = "cell" | "toolbar";

function buildButton(scope: Scope, key: string, placement: Placement = "cell"): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.className = BTN_CLASS;
  btn.type = "button";
  btn.dataset.phxKey = key; // used to detect a stale button when the area changes
  btn.dataset.phxPlace = placement; // so a scan can tell a fallback from the real thing
  const toolbar = placement === "toolbar";
  // Fixed compact size: in a cell it shares the narrow icon column (often with
  // the name too), so a variable-width label would overlap the name. Margin on
  // both sides keeps it off the icon. The MESA/create meaning is in the tooltip.
  Object.assign(btn.style, {
    cursor: "pointer",
    boxSizing: "border-box",
    width: toolbar ? "34px" : "26px",
    height: toolbar ? "34px" : "30px",
    minWidth: toolbar ? "34px" : "26px",
    flex: "0 0 auto",
    padding: "0",
    border: "1px solid var(--divider-color)",
    borderRadius: toolbar ? "8px" : "6px",
    font: "inherit",
    fontSize: toolbar ? "16px" : "15px",
    fontWeight: "700",
    lineHeight: "1",
    // In the toolbar the neighbours carry their own generous padding, so the
    // wide cell margin would read as a gap rather than as grouping.
    margin: toolbar ? "0 4px" : "0 10px",
    alignSelf: toolbar ? "center" : "auto",
    // Sit above HA's icon/name, whose padded hit areas otherwise overlap and
    // "steal" clicks meant for this button.
    position: "relative",
    zIndex: "20",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    verticalAlign: "middle",
    overflow: "hidden",
  } as CSSStyleDeclaration);
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openModal(scope, key).catch((err) => log("modal open failed", err));
  });
  applyButtonState(btn, scope, key);
  return btn;
}

/** What to call this target in the button's tooltip.
 *
 *  A device id is an opaque 32-character registry value, so the raw key says
 *  nothing and the tooltip has to ask Home Assistant what the device is called,
 *  preferring the operator's own rename the way HA's own UI does. Every other
 *  scope's key is already the readable thing an admin recognises, and falling
 *  back to the key keeps the label useful when the lookup finds nothing.
 */
function displayKey(scope: Scope, key: string): string {
  if (scope !== "device") return key;
  try {
    const device = getHass()?.devices?.[key];
    return device?.name_by_user || device?.name || key;
  } catch {
    return key;
  }
}

/** Set the +/MESA appearance for the entity's current profiled state. No-op (no
 *  DOM writes) when already in the right state, so our own table observer does
 *  not feed back into an endless rescan loop. */
function applyButtonState(btn: HTMLButtonElement, scope: Scope, key: string): void {
  const has = profiled[scope].has(key);
  const want = has ? "has" : "new";
  if (btn.dataset.phxState === want) return;
  dbg("glyph flip", key, `${btn.dataset.phxState ?? "(new)"} -> ${want}`, "has=", has);
  btn.dataset.phxState = want;
  // Compact, fixed-width glyphs (no "MESA" text, which is too wide for the cell):
  // a check on an accent fill means "profile set, click to edit"; a "+" outline
  // means "create". The MESA wording lives in the tooltip.
  btn.textContent = has ? "✓" : "+";
  const shown = displayKey(scope, key);
  const label = has
    ? t("inject.profileSet", { key: shown })
    : t("inject.profileCreate", { key: shown });
  btn.title = label;
  // The glyph alone ("+"/"check") is a useless accessible name, so mirror the
  // title into aria-label (which the title does not provide on its own).
  btn.setAttribute("aria-label", label);
  btn.style.background = has ? "var(--primary-color, #03a9f4)" : "var(--secondary-background-color, rgba(127,127,127,0.16))";
  btn.style.color = has ? "var(--text-primary-color, #fff)" : "var(--secondary-text-color, #717171)";
  btn.style.borderColor = has ? "var(--primary-color, #03a9f4)" : "var(--divider-color)";
}

function decorateRow(row: HTMLElement): void {
  const entityId = extractEntityId(row);
  if (!entityId) return;
  const buttons = row.querySelectorAll<HTMLButtonElement>(`.${BTN_CLASS}`);
  if (buttons.length) {
    if (buttons.length > 1) dbg("DUPLICATE buttons", entityId, "count=", buttons.length);
    applyButtonState(buttons[0], "entity", entityId);
    return;
  }
  const point = nameInsertionPoint(row);
  if (!point) return; // icon not rendered yet; a later scan will retry
  dbg("insert button", entityId, "has=", profiled.entity.has(entityId));
  // Mark this cell so only decorated (entity) cells get the wider column, never
  // the full-width group-header rows.
  point.parent.setAttribute("data-phx-widen", "1");
  point.parent.insertBefore(buildButton("entity", entityId), point.before);
}

/** Widen the first (icon) column of a data table so our button has room and does
 *  not collide with the name. Injected once per table shadow root. Targets both
 *  the header and body first cells so the columns stay aligned. This is the one
 *  deliberately invasive bit (it overrides HA's column width), so it is kept
 *  narrowly scoped and easy to remove if HA changes the table. */
function ensureColumnWidth(sr: ShadowRoot): void {
  if (sr.querySelector(`#${WIDEN_STYLE_ID}`)) return;
  const style = document.createElement("style");
  style.id = WIDEN_STYLE_ID;
  // Body: only cells we actually decorate (marked with data-phx-widen), so the
  // full-width group-header rows (e.g. "Ungrouped") are left alone. Header: the
  // first column header, to keep the columns aligned.
  style.textContent =
    "[data-phx-widen],.mdc-data-table__header-cell:first-child{" +
    "width:96px !important;min-width:96px !important;max-width:96px !important;flex:0 0 96px !important;}";
  sr.appendChild(style);
}

// The subpage scopes, each resolving its key from the URL. Explicit rather than
// an else-branch: scanAreaPage used to be the fallback for every non-table page,
// so a device page reached it and only escaped because areaIdFromPath returned
// null. A new surface added that way would silently claim the wrong scope.
function scan(): void {
  if (onEntityPage()) {
    scanDataTables();
    return;
  }
  for (const { scope, keyFromPath } of SUBPAGE_SURFACES) {
    const key = keyFromPath();
    if (key) {
      scanSubpage(scope, key);
      return;
    }
  }
}

function scanDataTables(): void {
  for (const table of deepQueryAll("ha-data-table")) {
    const sr = (table as HTMLElement).shadowRoot;
    if (!sr) continue;
    observeRoot(sr);
    ensureColumnWidth(sr);
    const rows = sr.querySelectorAll<HTMLElement>('[role="row"]');
    for (const row of Array.from(rows)) {
      if (row.classList.contains("mdc-data-table__header-row")) continue;
      try {
        decorateRow(row);
      } catch (e) {
        log("row decorate failed", e);
      }
    }
  }
}

/** A detail page: put the control in the header's ACTION row, beside the page's
 *  own edit and overflow buttons. The key comes from the URL, so there is no
 *  DOM id to extract.
 *
 *  Those actions are LIGHT-DOM children of hass-subpage carrying
 *  slot="toolbar-icon", which the component projects to the right of the
 *  toolbar; adding one more is the same mechanism Home Assistant's own pages
 *  use. The title is the FALLBACK, for a subpage with no actions of its own,
 *  which is the only reason the control ever appeared next to the name.
 *
 *  The stale-button check is load-bearing: navigating between two pages of the
 *  same kind reuses the DOM, so a button left from the previously viewed target
 *  would keep its old key and edit the wrong profile. It looks in BOTH
 *  placements, so a page that gains or loses its action row between scans
 *  cannot strand a copy in the other one. */
function scanSubpage(scope: Exclude<Scope, "entity">, key: string): void {
  for (const sp of deepQueryAll("hass-subpage")) {
    const host = sp as HTMLElement;
    const sr = host.shadowRoot;
    if (!sr) continue;
    const title = sr.querySelector<HTMLElement>(".main-title");
    // BOTH spellings, because this is Home Assistant's name and not ours: the
    // slot is "toolbar-icon" (singular) on the version verified here, and the
    // plural is the obvious thing to assume and was wrong. Matching either means
    // a rename in one direction degrades to the title fallback rather than to a
    // missing control. querySelector returns the first in DOCUMENT order, so a
    // page mixing them still yields its leading action.
    const firstAction = host.querySelector<HTMLElement>(
      ':scope > [slot="toolbar-icon"], :scope > [slot="toolbar-icons"]',
    );
    if (!title && !firstAction) continue;
    // Direct children only on the host: a data-table subpage has our own row
    // buttons deeper inside, and a loose descendant search would adopt one.
    const existing =
      host.querySelector<HTMLButtonElement>(`:scope > .${BTN_CLASS}`) ??
      title?.querySelector<HTMLButtonElement>(`.${BTN_CLASS}`) ??
      null;
    // Home Assistant renders the header's action buttons asynchronously, the same
    // way it renders row icons (see nameInsertionPoint), so an early scan can find
    // no action row and legitimately fall back to the title. Comparing the key
    // ALONE then made that fallback permanent: every later scan matched and
    // returned, so the control stayed beside the name even once the action row
    // existed. Placement is part of "is this button still right".
    // "cell" is what the title fallback builds (it wants the compact styling),
    // so the names must agree here or every scan would rebuild the button.
    const preferred: Placement = firstAction ? "toolbar" : "cell";
    if (existing && existing.dataset.phxKey === key && existing.dataset.phxPlace === preferred) {
      applyButtonState(existing, scope, key);
      return;
    }
    if (existing) existing.remove();
    observeRoot(sr);
    if (firstAction) {
      const btn = buildButton(scope, key, "toolbar");
      // Whatever the page's own actions use, so this lands in the same slot
      // rather than in a name only one HA version answers to.
      btn.slot = firstAction.getAttribute("slot") || "toolbar-icon";
      // Before the page's own actions: the overflow menu is conventionally
      // last, so appending would put this after it.
      host.insertBefore(btn, firstAction);
    } else if (title) {
      title.appendChild(buildButton(scope, key));
    }
    return; // only the active subpage
  }
}

const _scanReasons = new Set<string>();
let _scanCount = 0;
let _scanWindow = 0;
function debouncedScan(reason = "?"): void {
  if (disposed) return;
  _scanReasons.add(reason);
  if (debounceTimer !== undefined) {
    window.clearTimeout(debounceTimer);
    timeouts.delete(debounceTimer);
  }
  debounceTimer = trackedTimeout(() => {
    debounceTimer = undefined;
    if (DEBUG) {
      const now = Date.now();
      _scanCount++;
      if (now - _scanWindow >= 1000) {
        dbg("scans last ~1s:", _scanCount, "triggers:", [..._scanReasons]);
        _scanCount = 0;
        _scanWindow = now;
        _scanReasons.clear();
      }
    }
    safe(scan);
  }, DEBOUNCE_MS);
}

/** Observe a table's shadow root so virtualized row changes repaint promptly. */
function observeRoot(sr: ShadowRoot): void {
  if (observedRoots.has(sr)) return;
  observedRoots.add(sr);
  try {
    const observer = new MutationObserver((records) => {
      // Ignore our own button/style writes so applyButtonState's glyph swap cannot
      // ping-pong with HA's row re-render into an endless loop (the "+/check
      // toggles until reload" bug after adding or deleting a profile).
      if (!isSelfMutation(records)) debouncedScan("table-observer");
    });
    observer.observe(sr, {
      childList: true,
      subtree: true,
    });
    observers.add(observer);
  } catch (e) {
    log("observe failed", e);
  }
}

async function refreshProfiled(): Promise<void> {
  try {
    const set = new Set<string>();
    let cursor: string | undefined;
    for (let i = 0; i < 20; i++) {
      const resp = await api.listMesaProfiles({ limit: 200, cursor });
      for (const p of resp.profiles) set.add(p.entity_id);
      if (!resp.has_more || !resp.next_cursor) break;
      cursor = resp.next_cursor;
    }
    replaceProfiled("entity", set);
    dbg("refreshProfiled done, entities=", set.size, [...set].slice(0, 12));
  } catch (e) {
    log("profile list refresh failed", e);
  }
}

/** Swap a scope's key set in place: the Record is const so its entries are the
 *  stable identity every button reads through. */
function replaceProfiled(scope: Scope, keys: Iterable<string>): void {
  const set = profiled[scope];
  set.clear();
  for (const key of keys) set.add(key);
}

/** The scoped levels each fetch one unpaginated list, unlike entity profiles.
 *  A failure leaves the previous set alone rather than emptying it, so a
 *  transient error does not blank every button on the page. */
const SCOPED_REFRESH: Record<Exclude<Scope, "entity">, () => Promise<string[]>> = {
  device: async () => (await api.listMesaDevices()).devices.map((d) => d.device_id),
  area: async () => (await api.listMesaAreas()).areas.map((a) => a.area_id),
  integration: async () => (await api.listMesaIntegrations()).integrations.map((i) => i.integration),
};

async function refreshScoped(scope: Exclude<Scope, "entity">): Promise<void> {
  try {
    replaceProfiled(scope, await SCOPED_REFRESH[scope]());
  } catch (e) {
    log(`${scope} profile list refresh failed`, e);
  }
}

async function openModal(scope: Scope, key: string): Promise<void> {
  await import("./QuickAdd"); // defines <phx-mesa-quick-add>
  const el = document.createElement("phx-mesa-quick-add");
  el.setAttribute("scope", scope);
  el.setAttribute("key", key);
  // The editor renders the locked target and its own title from this, because
  // it has no picker source here to look a name up in. Without it a device
  // profile opened from an HA page is titled with a 32-character hex id.
  el.setAttribute("label", displayKey(scope, key));
  if (profiled[scope].has(key)) el.setAttribute("has-profile", "1");
  el.addEventListener("phx-mesa-saved", () => {
    dbg("phx-mesa-saved", scope, key);
    (scope === "entity" ? refreshProfiled() : refreshScoped(scope)).then(() => debouncedScan("saved"));
  });
  document.body.appendChild(el);
}

function safe(fn: () => void): void {
  if (disposed) return;
  try {
    fn();
  } catch (e) {
    log("error", e);
  }
}

function installListeners(): void {
  for (const ev of ["location-changed", "popstate", "hashchange"]) {
    const handler = () => debouncedScan(ev);
    window.addEventListener(ev, handler);
    listeners.push({ type: ev, handler });
  }
  // Coarse observer for navigation/panel swaps (shadow-internal changes are
  // covered by the per-table observers and the poll).
  try {
    const observer = new MutationObserver(() => debouncedScan("body-observer"));
    observer.observe(document.body, {
      childList: true,
      subtree: true,
    });
    observers.add(observer);
  } catch (e) {
    log("body observe failed", e);
  }
  trackedInterval(() => debouncedScan("poll"), POLL_MS);
}

let startAttempts = 0;
function start(): void {
  if (disposed) return;
  const hass = getHass();
  if (!hass) {
    // <home-assistant> not ready yet; retry briefly, then give up.
    if (startAttempts++ < 40) trackedTimeout(() => safe(start), 250);
    return;
  }
  if (!hass.user?.is_admin) return; // non-admins get nothing
  setHass(hass);
  // Opportunistic card-catalog harvest. This module loads on EVERY HA page, so
  // it sees dashboards where the Lovelace resources are already imported, which
  // keeps the catalog fresher than the panel alone can. Rate-limited internally
  // and fire-and-forget: it must never delay or fail the profile injection this
  // module actually exists to do.
  void syncCardCatalog();
  // Strings ride along with the profile reads this already waits on, so the
  // first scan paints its buttons with labels rather than raw keys.
  Promise.all([
    refreshProfiled(),
    ...SUBPAGE_SURFACES.map(({ scope }) => refreshScoped(scope)),
    loadTranslations(hass, resolveLanguage(hass)),
  ]).then(() => safe(scan));
  // These buttons are painted once per scan, so without this their labels stay
  // in the old language until the next full page load.
  const languageHandler = () => {
    void loadTranslations(getHass(), resolveLanguage(getHass())).then(() => safe(scan));
  };
  window.addEventListener("phx-language-changed", languageHandler);
  listeners.push({ type: "phx-language-changed", handler: languageHandler });
  // "auto" tracks the HA profile language, which changes without a page load.
  trackedInterval(() => {
    void syncTranslations(getHass()).then((changed) => { if (changed) safe(scan); });
  }, 5000);
  installListeners();
  // The build rides along because a redeploy can leave more than one copy of
  // this module in a page (see the stand-down guard below), and without it the
  // log cannot say WHICH build won and is driving the buttons.
  log("active", "build", JS_BUILD);
}

// HA can load a cache-busted replacement without unloading the old module. The
// newest build takes ownership and explicitly tears down the old build's
// observers, timers, listeners and DOM, so a live deploy does not require a full
// browser reload and two profile snapshots cannot fight over the same button.
const ACTIVE_FLAG = "__phxMesaInjectActive";
const controllerHost = window as unknown as Record<string, unknown>;
let controller: InjectController;
controller = {
  build: JS_BUILD,
  dispose: () => {
    if (disposed) return;
    disposed = true;
    if (debounceTimer !== undefined) window.clearTimeout(debounceTimer);
    for (const id of timeouts) window.clearTimeout(id);
    for (const id of intervals) window.clearInterval(id);
    for (const observer of observers) observer.disconnect();
    for (const { type, handler } of listeners) window.removeEventListener(type, handler);
    timeouts.clear();
    intervals.clear();
    observers.clear();
    listeners.length = 0;
    observedRoots = new WeakSet<ShadowRoot>();
    for (const element of deepQueryAll(`.${BTN_CLASS},#${WIDEN_STYLE_ID}`)) element.remove();
    for (const element of deepQueryAll("[data-phx-widen]")) {
      element.removeAttribute("data-phx-widen");
    }
    if (controllerHost[ACTIVE_FLAG] === controller) delete controllerHost[ACTIVE_FLAG];
  },
};

if (claimInjectController(controllerHost, ACTIVE_FLAG, controller)) {
  safe(start);
} else {
  disposed = true;
  log("an equal or newer injector build is already active; standing down");
}
