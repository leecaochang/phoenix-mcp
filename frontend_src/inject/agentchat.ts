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

async function ensureMod(): Promise<typeof import("./AgentChatWindow")> {
  if (!winMod) winMod = await import("./AgentChatWindow");
  return winMod;
}

async function open(tokenId?: string): Promise<void> {
  try {
    (await ensureMod()).showAgentChat(tokenId);
  } catch (e) {
    // eslint-disable-next-line no-console
    console.debug(LOG, "open failed", e);
  }
}

async function summon(tokenId?: string): Promise<void> {
  const durable = getAgentCliDurable();
  patchAgentCliDurable(agentCliOpenPatch(durable, tokenId, {
    w: window.innerWidth,
    h: window.innerHeight,
  }));
  await open(tokenId);
}

async function restore(): Promise<void> {
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

let attempts = 0;
function start(): void {
  const hass = getHass();
  if (!hass) {
    // <home-assistant> not ready yet; retry briefly, then give up.
    if (attempts++ < 40) window.setTimeout(start, 250);
    return;
  }
  if (!hass.user?.is_admin) return; // non-admins get nothing
  (window as any).__phxAgentChat = { ready: true, open: summon, close, toggle };
  registerAgentChatShortcut(getHass, toggle);
  // Opportunistic card-catalog harvest, same reasoning as the profile injector:
  // this loads on every HA page, so it catches dashboards where the Lovelace
  // resources are already imported. DYNAMICALLY imported and deferred so the
  // always-loaded footprint this module promises stays tiny; the harvester pulls
  // in api.ts, which is far larger than everything here. Rate-limited internally.
  window.setTimeout(() => {
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

// A page can end up with more than one copy of this module (a redeploy bumps the
// cache-bust ?v= and HA live-updates the module URL without a full reload). Guard
// so only the first copy installs the bridge.
const FLAG = "__phxAgentChatBootstrapped";
if ((window as any)[FLAG]) {
  // eslint-disable-next-line no-console
  console.debug(LOG, "another instance already active; standing down");
} else {
  (window as any)[FLAG] = true;
  try {
    start();
  } catch (e) {
    // eslint-disable-next-line no-console
    console.debug(LOG, "start failed", e);
  }
}
