/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Global Agent Chat bootstrap.
 *
 * Loaded on every HA page (via the frontend extra-module mechanism) only when the
 * admin turned on `agentcli_global`. It exposes `window.__phxAgentChat` so the Phoenix MCP
 * panel can open/close a chat window that floats over the whole HA UI. The heavy
 * window (React + AgentCliWindow + panel CSS) is lazy-imported on first open, so
 * the always-loaded footprint here is tiny.
 *
 * Design rules (mirroring the profile injector):
 *  - Admin only: does nothing unless `hass.user.is_admin`.
 *  - Shift+A toggles it when Home Assistant keyboard shortcuts are enabled.
 *  - If this never becomes ready (old HA, hass not found), the panel sees no
 *    `window.__phxAgentChat` and falls back to its own panel-only window.
 */

import { JS_BUILD } from "../version";
import { registerAgentChatShortcut } from "../utils/agentchat_shortcut";
import { claimInjectController, type InjectController } from "./ownership";
import {
  agentCliOpenPatch,
  getDurable as getAgentCliDurable,
  patchDurable as patchAgentCliDurable,
} from "../utils/agentcli_state";

const LOG = "[Phoenix MCP agentchat]";

function getHass(): any {
  return (document.querySelector("home-assistant") as any)?.hass ?? null;
}

let winMod: typeof import("./AgentChatWindow") | null = null;
let disposed = false;
const timeouts = new Set<number>();
let unregisterShortcut: (() => void) | null = null;
let bridge: {
  ready: boolean;
  open: typeof summon;
  restore: typeof restore;
  close: typeof close;
  toggle: typeof toggle;
  isVisible: () => boolean;
} | null = null;

function trackedTimeout(fn: () => void, delay: number): number {
  const id = window.setTimeout(() => {
    timeouts.delete(id);
    if (!disposed) fn();
  }, delay);
  timeouts.add(id);
  return id;
}

async function ensureMod(): Promise<typeof import("./AgentChatWindow")> {
  if (!winMod) winMod = await import("./AgentChatWindow");
  return winMod;
}

async function open(tokenId?: string): Promise<void> {
  try {
    const module = await ensureMod();
    if (!disposed) module.showAgentChat(tokenId);
  } catch (e) {
    // eslint-disable-next-line no-console
    console.debug(LOG, "open failed", e);
  }
}

async function summon(tokenId?: string): Promise<void> {
  if (disposed) return;
  // If the mounted chat lives in its popup, this button press is the browser
  // user gesture that can bring that window forward. Do it synchronously before
  // any async import boundary or durable geometry update consumes the gesture.
  if (tokenId === undefined && winMod?.focusAgentChatPopup()) return;
  const durable = getAgentCliDurable();
  patchAgentCliDurable(agentCliOpenPatch(durable, tokenId, {
    w: window.innerWidth,
    h: window.innerHeight,
  }));
  await open(tokenId);
}

async function restore(): Promise<void> {
  if (disposed) return;
  // Shift+A is a visibility toggle, so reopening restores the last dragged
  // location instead of behaving like the header button's centered summon.
  patchAgentCliDurable(agentCliOpenPatch(getAgentCliDurable()));
  await open();
}

function close(): void {
  // If the window module was never loaded, there is nothing open to close.
  winMod?.hideAgentChat();
}

async function toggle(): Promise<void> {
  if (winMod?.isAgentChatVisible()) {
    close();
    return;
  }
  await restore();
}

function start(): void {
  if (disposed) return;
  const hass = getHass();
  if (!hass?.user) {
    // The root and its partial hass object can exist before browser
    // authentication finishes. Wait for the user instead of permanently
    // falling back to the panel after an arbitrary startup deadline.
    trackedTimeout(start, 250);
    return;
  }
  if (!hass.user.is_admin) return; // non-admins get nothing
  bridge = {
    ready: true,
    open: summon,
    restore,
    close,
    toggle,
    isVisible: () => Boolean(winMod?.isAgentChatVisible()),
  };
  (window as any).__phxAgentChat = bridge;
  unregisterShortcut = registerAgentChatShortcut(getHass, toggle);
  // Opportunistic card-catalog harvest, same reasoning as the profile injector:
  // this loads on every HA page, so it catches dashboards where the Lovelace
  // resources are already imported. DYNAMICALLY imported and deferred so the
  // always-loaded footprint this module promises stays tiny; the harvester pulls
  // in api.ts, which is far larger than everything here. Rate-limited internally.
  trackedTimeout(() => {
    import("../utils/card_harvest")
      .then((m) => m.syncCardCatalog())
      .catch(() => { /* harvesting is best effort and never blocks the chat bridge */ });
  }, 3000);
  // The Phoenix MCP panel may already be rendering (this bootstrap polls for hass, so
  // it can win or lose that race); announce readiness so the panel can switch
  // from its fallback window as state, not on some later unrelated re-render.
  window.dispatchEvent(new CustomEvent("phx-agentchat-ready"));
  // eslint-disable-next-line no-console
  console.debug(LOG, "ready", "build", JS_BUILD);
}

// A cache-busted replacement can enter an already-open page. The newest build
// disposes the prior shortcut, retry/harvest timers, bridge and mounted window
// before taking ownership.
const FLAG = "__phxAgentChatBootstrapped";
const controllerHost = window as unknown as Record<string, unknown>;
let controller: InjectController;
controller = {
  build: JS_BUILD,
  dispose: () => {
    if (disposed) return;
    disposed = true;
    for (const id of timeouts) window.clearTimeout(id);
    timeouts.clear();
    unregisterShortcut?.();
    unregisterShortcut = null;
    winMod?.hideAgentChat();
    if ((window as any).__phxAgentChat === bridge) delete (window as any).__phxAgentChat;
    bridge = null;
    if (controllerHost[FLAG] === controller) delete controllerHost[FLAG];
  },
};

if (!claimInjectController(controllerHost, FLAG, controller)) {
  disposed = true;
  // eslint-disable-next-line no-console
  console.debug(LOG, "an equal or newer build is already active; standing down");
} else {
  try {
    start();
  } catch (e) {
    // eslint-disable-next-line no-console
    console.debug(LOG, "start failed", e);
  }
}
