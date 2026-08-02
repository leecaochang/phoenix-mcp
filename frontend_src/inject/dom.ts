/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Pure DOM helpers for the injector, separated so they can be unit-tested without
 * the entry module's load-time side effects.
 *
 * THE ONE FRAGILE, HA-VERSION-SPECIFIC FUNCTION is `extractEntityId`. If a future
 * HA release changes how ha-data-table exposes a row's id, update only this file
 * (and bump MESA_INJECT_MIN_HA in const.py if appropriate).
 */

export const ENTITY_ID_RE = /^[a-z_]+\.[a-z0-9_]+$/;
export const BTN_CLASS = "phx-mesa-inject-btn";
// id of the per-table <style> we inject to widen the icon column. Lives here so
// isSelfMutation can recognise it as one of our own nodes.
export const WIDEN_STYLE_ID = "phx-mesa-col-widen";

// URL prefixes whose data-table rows are entity rows we can profile. The
// /config/entities list covers every entity (including person.*), so it is the
// broadest surface; the per-domain pages are kept for in-context convenience.
export const ENTITY_PAGE_PREFIXES = [
  "/config/entities",
  "/config/automation",
  "/config/script",
  "/config/scene",
  "/config/helpers",
  "/config/person",
];

export function onEntityPage(path: string = window.location.pathname): boolean {
  return ENTITY_PAGE_PREFIXES.some((p) => path.startsWith(p));
}

// The three subpage surfaces, all keyed from the URL rather than the DOM, which
// makes them far less fragile than the data-table adapter below: a detail page's
// path is stable in a way its markup is not.
const AREA_DETAIL_RE = /^\/config\/areas\/area\/([^/]+)/;
const DEVICE_DETAIL_RE = /^\/config\/devices\/device\/([^/]+)/;
const INTEGRATION_DETAIL_RE = /^\/config\/integrations\/integration\/([^/]+)/;

/** The area_id from an area detail page URL, or null when not on one. */
export function areaIdFromPath(path: string = window.location.pathname): string | null {
  const m = path.match(AREA_DETAIL_RE);
  return m ? decodeURIComponent(m[1]) : null;
}

/** The device_id from a device detail page URL, or null when not on one. */
export function deviceIdFromPath(path: string = window.location.pathname): string | null {
  const m = path.match(DEVICE_DETAIL_RE);
  return m ? decodeURIComponent(m[1]) : null;
}

/** The integration domain from an integration detail page URL, or null. */
export function integrationDomainFromPath(path: string = window.location.pathname): string | null {
  const m = path.match(INTEGRATION_DETAIL_RE);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Which scope owns which detail page, in the order the scan tries them.
 *
 * Pure data, and here rather than in the entry module so it can be tested: the
 * pairing is the one part of this that type-checks perfectly when wrong. A scope
 * pointed at another scope's extractor still compiles, and the result is a
 * button that either never appears or appears with the wrong level, which is
 * exactly the class of mistake nothing else would report.
 */
export const SUBPAGE_SURFACES: {
  scope: "device" | "area" | "integration";
  keyFromPath: (path?: string) => string | null;
  samplePath: string;
}[] = [
  { scope: "device", keyFromPath: deviceIdFromPath, samplePath: "/config/devices/device/abc123" },
  { scope: "area", keyFromPath: areaIdFromPath, samplePath: "/config/areas/area/kitchen" },
  { scope: "integration", keyFromPath: integrationDomainFromPath, samplePath: "/config/integrations/integration/hue" },
];

/**
 * Where to insert the control: in the same cell as the entity icon
 * (ha-state-icon), right after it. The icon is the one anchor that is reliably in
 * the leftmost entity/name column on every HA config table, so co-locating with
 * it keeps the control in the right column regardless of how the name renders.
 *
 * Returns null when there is no icon yet. HA renders row icons asynchronously
 * (virtualized rows), so injecting before the icon exists would drop the control
 * at the cell's start and the icon would then render after it (the cause of the
 * left/right "swap"). Refusing here makes a later scan retry once the icon is in.
 */
export function nameInsertionPoint(row: HTMLElement): { parent: HTMLElement; before: Node | null } | null {
  const icon = row.querySelector<HTMLElement>("ha-state-icon, ha-icon");
  const iconCell = icon ? icon.closest<HTMLElement>('[role="cell"]') : null;
  if (icon && iconCell) {
    return { parent: iconCell, before: icon.nextSibling };
  }
  return null;
}

/** Is this node one we injected (our +/MESA button or our column-width style)? */
function isOurNode(n: Node): boolean {
  const el = n as HTMLElement;
  return el.nodeType === 1 && (el.classList?.contains(BTN_CLASS) || el.id === WIDEN_STYLE_ID);
}

/**
 * True when every mutation in the batch was caused by our own injected DOM, so
 * reacting to it would feed an endless rescan -> repaint -> rescan loop.
 *
 * The per-table observer fires on any childList change in the table's shadow
 * root, including our own writes: inserting the button, swapping its glyph in
 * applyButtonState, and appending the width <style>. applyButtonState is guarded
 * against redundant writes, but a real state change (right after an add or delete)
 * still mutates the DOM, the observer sees it, and that ping-pongs with HA's own
 * row re-render until the page is reloaded (the "+/check toggles forever" bug).
 * Filtering our own mutations here breaks our half of that loop.
 *
 * A record that REMOVES our button is NOT self: HA reclaimed the cell, and we
 * want a later scan to re-add the button.
 */
export function isSelfMutation(records: MutationRecord[]): boolean {
  for (const r of records) {
    const target = r.target as HTMLElement;
    // Glyph/text churn on our own button (applyButtonState swapping + and check).
    if (target?.nodeType === 1 && target.classList?.contains(BTN_CLASS)) continue;
    // Our button or width-style being ADDED by us. Pure additions only; a removal
    // means HA dropped our node, which we must react to.
    if (
      r.removedNodes.length === 0 &&
      r.addedNodes.length > 0 &&
      Array.from(r.addedNodes).every(isOurNode)
    ) {
      continue;
    }
    return false; // a foreign mutation: worth a rescan
  }
  return true;
}

/** Collect all elements matching `selector`, piercing open shadow roots. */
export function deepQueryAll(selector: string, root: Document | ShadowRoot | Element = document): Element[] {
  const out: Element[] = [];
  const stack: (Document | ShadowRoot | Element)[] = [root];
  while (stack.length) {
    const node = stack.pop()!;
    let descendants: Element[] = [];
    try {
      descendants = Array.from((node as Element).querySelectorAll("*"));
    } catch {
      continue;
    }
    for (const el of descendants) {
      if (el.matches?.(selector)) out.push(el);
      const sr = (el as HTMLElement).shadowRoot;
      if (sr) stack.push(sr);
    }
  }
  return out;
}

/**
 * Read an entity_id for one data-table row.
 *
 * HA's ha-data-table stores each row's configured id as a `.rowId` property on
 * the `role="row"` element (the same property its own row-click handler reads).
 * On the entity picker pages that id is the entity_id. We validate the shape, so a
 * non-entity table (numeric id, config-entry id, etc.) yields null and is skipped.
 * Returns null when no plausible entity_id is found, which makes the whole feature
 * self-disable on a DOM it does not recognise.
 */
export function extractEntityId(row: HTMLElement): string | null {
  const prop = (row as any).rowId;
  if (typeof prop === "string" && ENTITY_ID_RE.test(prop)) return prop;
  for (const attr of ["data-row-id", "data-id", "data-entity-id"]) {
    const v = row.getAttribute(attr);
    if (v && ENTITY_ID_RE.test(v)) return v;
  }
  return null;
}
