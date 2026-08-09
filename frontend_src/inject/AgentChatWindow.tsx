/** Global Agent Chat window, mounted outside the Phoenix MCP panel so it can float over
 * the whole HA UI. Lazy-loaded by inject/agentchat.ts on first open. Mirrors the
 * QuickAdd pattern: a fixed, click-through host with its own shadow root carrying
 * the panel CSS (so :host theme variables cascade to the window), into which the
 * real AgentCliWindow is mounted. All data comes from the same admin API the panel
 * uses, authenticated via the page hass.
 */
import { useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { api, setHass } from "../api";
import { loadTranslations, resolveLanguage, syncTranslations } from "../i18n";
import { AgentCliWindow } from "../components/AgentCliWindow";
import type { AgentCliInstance, TokenRecord } from "../types";
import { JS_BUILD } from "../version";
import { patchDurable as patchAgentCliDurable } from "../utils/agentcli_state";
import PANEL_CSS from "../phoenix-mcp-panel.css?inline";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function getHass(): any {
  return (document.querySelector("home-assistant") as any)?.hass ?? null;
}

interface BootData {
  tokens: TokenRecord[];
  instances: AgentCliInstance[];
  scrollback: number;
}

function Boot({
  tokenId, onClose, summonVersion,
}: {
  tokenId: string;
  onClose: () => void;
  summonVersion: number;
}) {
  const [data, setData] = useState<BootData | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.listTokens(), api.getAgentCliProviders(), api.getSettings()])
      .then(([tokens, prov, settings]) => {
        if (cancelled) return;
        if (settings.kill_switch) {
          onClose();
          return;
        }
        setData({ tokens, instances: prov.instances, scrollback: settings.agentcli_scrollback_lines });
      })
      .catch(() => { if (!cancelled) onClose(); });  // can't load (e.g. kill switch) -> close
    return () => { cancelled = true; };
  }, [reloadKey, onClose]);

  // Under "auto" the language follows the HA profile, which can change while
  // this window is open. HA re-renders in place rather than reloading, so
  // without a poll the window keeps the language it opened with. Cheap: a
  // string compare until it actually differs.
  useEffect(() => {
    const id = window.setInterval(() => {
      void syncTranslations(getHass()).then((changed) => {
        if (changed) setReloadKey((k) => k + 1);
      });
    }, 2000);
    return () => window.clearInterval(id);
  }, []);

  // Keep the account list fresh if it changes in Settings while open, and the
  // token list fresh if a token is revoked elsewhere while this window is open
  // (AgentCliWindow below reselects/clears on a token list change).
  useEffect(() => {
    const refresh = () => setReloadKey((k) => k + 1);
    window.addEventListener("phx-agentcli-providers-changed", refresh);
    window.addEventListener("phx-tokens-changed", refresh);
    return () => {
      window.removeEventListener("phx-agentcli-providers-changed", refresh);
      window.removeEventListener("phx-tokens-changed", refresh);
    };
  }, []);

  if (!data) return null;  // brief; the window appears once its data has loaded
  return (
    <AgentCliWindow
      tokens={data.tokens}
      instances={data.instances}
      scrollbackLines={data.scrollback}
      initialTokenId={tokenId || data.tokens[0]?.id || ""}
      onClose={onClose}
      hass={getHass()}
      getHass={getHass}
      summonVersion={summonVersion}
    />
  );
}

let host: HTMLDivElement | null = null;
let reloadStrings: (() => void) | null = null;
let root: Root | null = null;
let themeObserver: MutationObserver | null = null;
let activeTokenId = "";
let summonVersion = 0;

function paintAgentChat(): void {
  root?.render(
    <Boot
      tokenId={activeTokenId}
      onClose={hideAgentChat}
      summonVersion={summonVersion}
    />,
  );
}

export function isAgentChatVisible(): boolean {
  return host !== null;
}

// Same resolution order as PhoenixPanelElement._applyThemeClass: the panel's
// Light/Dark/Auto setting (persisted to localStorage, readable from any HA
// page) wins; Auto follows HA's dark-mode flag; with neither available, no
// class is set and the CSS prefers-color-scheme default decides. The panel
// element itself cannot be queried for its resolved class here: HA mounts
// panels behind several nested shadow roots, so document.querySelector
// never reaches it.
function resolveThemeClass(): "phx-theme-dark" | "phx-theme-light" | null {
  let saved: string | null = null;
  try { saved = localStorage.getItem("phx-theme"); } catch { /* storage blocked */ }
  if (saved === "light") return "phx-theme-light";
  if (saved === "dark") return "phx-theme-dark";
  try {
    const dark = getHass()?.themes?.darkMode;
    if (dark === true) return "phx-theme-dark";
    if (dark === false) return "phx-theme-light";
  } catch { /* leave to prefers-color-scheme */ }
  return null;
}

function applyTheme(): void {
  if (!host) return;
  host.classList.remove("phx-theme-dark", "phx-theme-light");
  const cls = resolveThemeClass();
  if (cls) host.classList.add(cls);
}

// The window persists across HA navigation and theme flips, so a one-shot
// read at open goes stale. Re-resolve when the Phoenix MCP panel broadcasts its
// theme toggle (same-document CustomEvent; localStorage "storage" events
// only fire in OTHER tabs) and when HA re-applies its theme variables to
// <html> (which is how its dark-mode flip lands in the DOM).
function watchTheme(): void {
  window.addEventListener("phx-theme-changed", applyTheme);
  themeObserver = new MutationObserver(applyTheme);
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["style", "class"],
  });
}

function unwatchTheme(): void {
  window.removeEventListener("phx-theme-changed", applyTheme);
  themeObserver?.disconnect();
  themeObserver = null;
}

export function hideAgentChat(): void {
  // The panel's own close button persists open:false itself (closeAgentCli),
  // but this window's close button (and the load-failure fallback below) call
  // straight here with no other path through the panel, so this is the only
  // place that clears the flag for THIS window. Without it, closing here left
  // open:true stuck in storage, and a later reload reopened the window right
  // back up (the panel's own restore-on-mount reads that same flag).
  patchAgentCliDurable({ open: false });
  unwatchTheme();
  if (reloadStrings) {
    window.removeEventListener("phx-language-changed", reloadStrings);
    reloadStrings = null;
  }
  root?.unmount();
  root = null;
  host?.remove();
  host = null;
  activeTokenId = "";
  summonVersion = 0;
}

export function showAgentChat(tokenId?: string): void {
  if (tokenId !== undefined) activeTokenId = tokenId;
  if (host) {
    // Keep the mounted chat and its live conversation intact. A fresh render of
    // the same Boot tree only changes this signal; AgentCliWindow uses it to
    // unfold a visible minimized pill without restarting an in-flight turn.
    summonVersion += 1;
    paintAgentChat();
    return;
  }
  const hass = getHass();
  setHass(hass);
  // This bundle loads on any HA page, so it fetches its own catalog. The window
  // opens right away and repaints a round trip later with the strings in place;
  // paintAgentChat() no-ops if it was closed before then.
  void loadTranslations(hass, resolveLanguage(hass)).then(paintAgentChat);
  // The panel's language dropdown lives in a different element and can be used
  // while this window is open, so re-fetch on its event the same way the theme
  // is re-resolved below.
  reloadStrings = () => void loadTranslations(getHass(), resolveLanguage(getHass())).then(paintAgentChat);
  window.addEventListener("phx-language-changed", reloadStrings);
  host = document.createElement("div");
  // Fixed, full-viewport, but click-through and transparent: only the window
  // itself (pointer-events:auto) is interactive, so the HA page behind stays live.
  host.style.cssText =
    "position:fixed; inset:0; pointer-events:none; z-index:2147483000; background:transparent;";
  host.dataset.jsBuild = JS_BUILD;  // devtools-visible marker of the loaded chunk
  applyTheme();
  watchTheme();
  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = PANEL_CSS + "\n.agentcli-window{pointer-events:auto;}";
  shadow.appendChild(style);
  const mount = document.createElement("div");
  mount.style.pointerEvents = "none";
  shadow.appendChild(mount);
  document.body.appendChild(host);
  root = createRoot(mount);
  paintAgentChat();
}
