import React, { useState, useEffect, useCallback, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { TokenRecord, GlobalSettings, AgentCliInstance } from "./types";
import { AgentCliWindow, focusAgentCliPopup } from "./components/AgentCliWindow";
import { subscribeApprovalEvents } from "./utils/approval_events";
import {
  agentCliOpenPatch,
  getDurable as getAgentCliDurable,
  patchDurable as patchAgentCliDurable,
} from "./utils/agentcli_state";
import { TokenListView } from "./views/TokenList";
import { TokenDetailView } from "./views/TokenDetail";
import { AuditView } from "./views/AuditView";
import { SettingsView } from "./views/SettingsView";
import { ApprovalsView } from "./views/ApprovalsView";
import { MesaView } from "./views/MesaView";
import { ChangesView } from "./views/ChangesView";
import { OnboardingWizard } from "./views/OnboardingWizard";
import { api, setHass } from "./api";
import { syncCardCatalog } from "./utils/card_harvest";
import { registerAgentChatShortcut } from "./utils/agentchat_shortcut";
import {
  getLanguagePreference,
  isI18nReady,
  loadTranslations,
  resolveLanguage,
  setLanguagePreference,
  syncTranslations,
  t,
} from "./i18n";
import { Loading, ErrorMsg, RefreshIcon, DOCS_BASE_URL } from "./components/common";
import PANEL_CSS from "./phoenix-mcp-panel.css?inline";

type Tab = "tokens" | "approvals" | "changes" | "mesa" | "audit" | "settings";
type Theme = "light" | "dark" | "auto";

export { HIGH_RISK_DOMAINS } from "./utils";

// Re-exported for the many views that import these from "../index". The
// definitions live in components/common so reusing them outside the panel does
// not pull the whole app into a bundle.
export { Loading, ErrorMsg, RefreshIcon };

type View =
  | { name: "list" }
  | { name: "detail"; tokenId: string }
  | { name: "wizard" };

const TAB_LABEL_KEYS: Record<Tab, string> = { tokens: "shell.tabTokens", approvals: "shell.tabApprovals", changes: "shell.tabChanges", mesa: "shell.tabMesa", audit: "shell.tabAudit", settings: "shell.tabSettings" };

// Persist the active tab so it survives a reload or navigating away and back.
const TAB_STORAGE_KEY = "phx-tab";
function readStoredTab(): Tab {
  try {
    const v = localStorage.getItem(TAB_STORAGE_KEY);
    if (v && v in TAB_LABEL_KEYS) return v as Tab;
  } catch {
    // localStorage unavailable (e.g. blocked): fall back to the default.
  }
  return "tokens";
}

// Persist the token being edited so returning to the Tokens tab reopens its
// detail; with none stored (or any other tab), reopen at the Tokens list.
const TOKEN_DETAIL_STORAGE_KEY = "phx-token-detail";
function readStoredView(tab: Tab): View {
  if (tab !== "tokens") return { name: "list" };
  try {
    const id = localStorage.getItem(TOKEN_DETAIL_STORAGE_KEY);
    if (id) return { name: "detail", tokenId: id };
  } catch {
    // localStorage unavailable: fall back to the list.
  }
  return { name: "list" };
}

// Persist the Approvals sub-tab (Pending vs History) the same way the Tokens
// detail view is remembered, so reloading or navigating away and back keeps
// whichever one was open.
type ApprovalsTab = "pending" | "history";
const APPROVALS_TAB_STORAGE_KEY = "phx-approvals-tab";
function readStoredApprovalsTab(): ApprovalsTab {
  try {
    const v = localStorage.getItem(APPROVALS_TAB_STORAGE_KEY);
    if (v === "pending" || v === "history") return v;
  } catch {
    // localStorage unavailable: fall back to the default.
  }
  return "pending";
}

function PhoenixApp({ hass, narrow, theme, onThemeChange, language, onLanguageChange }: { hass: unknown; narrow: boolean; theme: Theme; onThemeChange: (t: Theme) => void; language: string; onLanguageChange: (lang: string) => void }) {
  const [tab, setTab] = useState<Tab>(readStoredTab);
  const [view, setView] = useState<View>(() => readStoredView(readStoredTab()));
  const [tokens, setTokens] = useState<TokenRecord[]>([]);
  const [settings, setSettings] = useState<GlobalSettings | null>(null);
  const [loadingTokens, setLoadingTokens] = useState(true);
  const [tokensError, setTokensError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [pendingCount, setPendingCount] = useState<number>(0);
  // Bumped on each HA approval event so the Approvals list can refresh instantly.
  const [approvalSignal, setApprovalSignal] = useState<number>(0);
  // Approvals whose saved action is executing right now. An approve runs its tool
  // inline in the admin's request, so nothing resolves for seconds; without this
  // the list keeps offering Approve and Reject on an approval already being acted
  // on, and the second click can only ever earn a 409.
  const [claimedApprovals, setClaimedApprovals] = useState<ReadonlySet<string>>(() => new Set());
  const [deepApprovalId, setDeepApprovalId] = useState<string | null>(null);
  const [approvalsTab, setApprovalsTab] = useState<ApprovalsTab>(readStoredApprovalsTab);
  const [agentCliOpen, setAgentCliOpen] = useState<boolean>(() => getAgentCliDurable().open);
  // Whether the global floating-window bootstrap (inject/agentchat.ts) has
  // installed itself. Held as STATE, not read live off window.__phxAgentChat in
  // render: the bootstrap becomes ready asynchronously (it polls for hass), and
  // a live read only got re-evaluated on unrelated re-renders, silently
  // unmounting an open fallback window at surprising times (e.g. a theme
  // toggle). The bootstrap dispatches phx-agentchat-ready when it installs.
  const [globalChatReady, setGlobalChatReady] = useState<boolean>(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => !!(window as any).__phxAgentChat?.ready,
  );
  // Bumped each time the window is opened from a button so it remounts and
  // re-reads the (re-centered, un-minimized) durable geometry.
  const [agentCliKey, setAgentCliKey] = useState<number>(0);
  const [agentCliInstances, setAgentCliInstances] = useState<AgentCliInstance[]>([]);
  const menuRef = useRef<HTMLElement | null>(null);
  // Baseline pendingCount "seen" while actually viewing Pending; null until the
  // first count is known, so a reload with History persisted and count > 0
  // never looks like a rise. Used to detect a genuinely NEW approval arriving
  // while History is open, distinct from "some approval happens to be pending".
  const lastSeenPendingCountRef = useRef<number | null>(null);
  const openAgentCliRef = useRef<(tokenId?: string) => void>(() => {});

  // Deep-link from a notification: /phoenix-mcp#approvals or /phoenix-mcp#approvals/{id} opens
  // the Approvals tab (and that specific approval's popup). We listen on
  // hashchange AND HA's SPA-navigation signals (location-changed, popstate):
  // when the panel is already open, HA's router navigates without a real
  // hashchange, so hashchange alone would miss the deep-link (F3).
  useEffect(() => {
    function handleHash() {
      const m = window.location.hash.replace(/^#/, "").match(/^approvals(?:\/(.+))?$/);
      if (!m) return;
      setTab("approvals");
      setView({ name: "list" });
      if (m[1]) {
        setDeepApprovalId(decodeURIComponent(m[1]));
        // A notification deep-link is for a pending approval, so show Pending
        // immediately: otherwise a persisted History sub-tab flashes until the
        // fetch resolves (ApprovalsView corrects to History if it is already
        // resolved). Fixes "the link took me to History".
        setApprovalsTab("pending");
        // Strip the approval id from the URL so a later reload or revisit does not
        // re-open this (by then resolved) approval from a stale hash.
        try {
          window.history.replaceState(null, "", window.location.pathname + window.location.search);
        } catch { /* URL API blocked: leave the hash */ }
      }
    }
    handleHash();
    window.addEventListener("hashchange", handleHash);
    window.addEventListener("location-changed", handleHash);
    window.addEventListener("popstate", handleHash);
    return () => {
      window.removeEventListener("hashchange", handleHash);
      window.removeEventListener("location-changed", handleHash);
      window.removeEventListener("popstate", handleHash);
    };
  }, []);

  // Build the dashboard card catalog once per panel visit. Backgrounded and
  // fully self-contained: it never blocks a render, never surfaces an error, and
  // rate-limits itself, because the catalog only changes when the operator
  // installs or removes a plugin. The panel is the reliable place for this since
  // only a browser can see which custom cards exist (utils/card_harvest).
  useEffect(() => {
    void syncCardCatalog();
  }, []);

  useEffect(() => {
    try { localStorage.setItem(TAB_STORAGE_KEY, tab); } catch { /* storage blocked: skip */ }
  }, [tab]);

  // Remember the open token detail (only on the Tokens tab); clear it otherwise
  // so returning lands on the list rather than a stale detail.
  useEffect(() => {
    try {
      if (tab === "tokens" && view.name === "detail") {
        localStorage.setItem(TOKEN_DETAIL_STORAGE_KEY, view.tokenId);
      } else {
        localStorage.removeItem(TOKEN_DETAIL_STORAGE_KEY);
      }
    } catch { /* storage blocked: skip */ }
  }, [tab, view]);

  useEffect(() => {
    try { localStorage.setItem(APPROVALS_TAB_STORAGE_KEY, approvalsTab); } catch { /* storage blocked: skip */ }
  }, [approvalsTab]);

  // Load configured agentCLI providers, open the floating chat window when a
  // "connect" surface requests it, and refresh the provider list whenever it
  // changes in Settings (so the window updates without a page reload).
  useEffect(() => {
    const refresh = () => api.getAgentCliProviders()
      .then((r) => setAgentCliInstances(r.instances)).catch(() => { /* not configured yet */ });
    refresh();
    // Opening from a token's connect flow binds the window to that token;
    // opening from the header restores the last-used token.
    // Routed through a ref, not the closure: this effect mounts once, and
    // openAgentCli depends on `settings`, which is still null on that first
    // render. Calling the captured copy would decide routing (kill switch,
    // global-vs-panel window) against settings that had not loaded yet, and
    // always fall through to the panel-only window.
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent).detail as { tokenId?: string } | undefined;
      openAgentCliRef.current(detail?.tokenId);
    };
    // A revoke elsewhere (Token Detail) should drop the token from the panel
    // fallback window's own tokens prop too, same as refreshTokens() already
    // does via goBack() when revoke happens through that path; listening here
    // covers it independent of navigation, matching the floating window.
    const onTokensChanged = () => { void refreshTokens(); };
    window.addEventListener("phx-open-agentcli", onOpen);
    window.addEventListener("phx-agentcli-providers-changed", refresh);
    window.addEventListener("phx-tokens-changed", onTokensChanged);
    return () => {
      window.removeEventListener("phx-open-agentcli", onOpen);
      window.removeEventListener("phx-agentcli-providers-changed", refresh);
      window.removeEventListener("phx-tokens-changed", onTokensChanged);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Open (or re-summon) the chat window: re-center it and clear any minimized
  // state, so a window that was dragged off screen or minimized always comes
  // back visible. Blocked while the kill switch is on. When the admin chose the
  // global-visibility mode and the inject module is ready, hand off to that
  // window (floats over all of HA); otherwise render the panel-only window (also
  // the fallback when global injection is unsupported). The key bump forces a
  // remount that re-reads the centered geometry.
  const openAgentCli = useCallback((tokenId?: string, preservePosition = false) => {
    if (settings?.kill_switch) return;
    // The panel-local fallback can also be popped out. Keep its live React tree
    // intact and use this direct button gesture to request popup focus.
    if (tokenId === undefined && focusAgentCliPopup()) return;
    const d = getAgentCliDurable();
    patchAgentCliDurable(agentCliOpenPatch(
      d,
      tokenId,
      preservePosition ? undefined : { w: window.innerWidth, h: window.innerHeight },
    ));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const g = (window as any).__phxAgentChat;
    if (settings?.agentcli_global && g?.ready) {
      g.open(tokenId);
      return;
    }
    setAgentCliKey((k) => k + 1);
    setAgentCliOpen(true);
  }, [settings]);
  const closeAgentCli = useCallback(() => {
    patchAgentCliDurable({ open: false });
    setAgentCliOpen(false);
  }, []);

  // The panel registers the same shortcut as the global bootstrap so Shift+A
  // also works when the admin chose the panel-only Agent Chat mode. The helper
  // follows HA's profile opt-in and input/selection guards.
  useEffect(() => {
    if (!settings || settings.kill_switch) return;
    return registerAgentChatShortcut(() => hass as { enableShortcuts?: boolean }, () => {
      // The injected listener normally handles global mode first. This branch
      // keeps the toggle correct if the panel listener happened to register first.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const globalChat = (window as any).__phxAgentChat;
      if (settings.agentcli_global && globalChat?.ready && globalChat.toggle) {
        void globalChat.toggle();
      } else if (agentCliOpen) {
        closeAgentCli();
      } else {
        openAgentCli(undefined, true);
      }
    });
  }, [agentCliOpen, closeAgentCli, hass, openAgentCli, settings]);

  // Keep the ref the phx-open-agentcli listener calls pointing at the current
  // openAgentCli, so an open request always routes against loaded settings.
  useEffect(() => { openAgentCliRef.current = openAgentCli; }, [openAgentCli]);

  useEffect(() => {
    const onReady = () => setGlobalChatReady(true);
    window.addEventListener("phx-agentchat-ready", onReady);
    // The bootstrap may have installed between the state initializer and this
    // listener attaching; re-read so the flip is never missed.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((window as any).__phxAgentChat?.ready) setGlobalChatReady(true);
    return () => window.removeEventListener("phx-agentchat-ready", onReady);
  }, []);

  // In global-visibility mode the floating window owns the chat, but on a fresh
  // page load its bootstrap can become ready AFTER this panel already restored
  // its fallback window. Hand the open fallback off to the floating window
  // instead of leaving a window the next re-render would silently unmount.
  useEffect(() => {
    if (globalChatReady && settings?.agentcli_global && agentCliOpen) {
      setAgentCliOpen(false);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__phxAgentChat?.open?.();
    }
  }, [globalChatReady, settings?.agentcli_global, agentCliOpen]);

  // The kill switch hides the window everywhere and blocks reopening until it is
  // turned off again. The toggle lives in this panel, so flipping it re-renders
  // here and fires this.
  useEffect(() => {
    if (settings?.kill_switch) {
      setAgentCliOpen(false);
      patchAgentCliDurable({ open: false });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__phxAgentChat?.close?.();
    }
  }, [settings?.kill_switch]);
  // While the user is actually looking at Pending, treat the current count as
  // "seen" so a later click while on History can tell a genuinely new arrival
  // apart from a count that was already nonzero.
  useEffect(() => {
    if (tab === "approvals" && approvalsTab === "pending") {
      lastSeenPendingCountRef.current = pendingCount;
    }
  }, [tab, approvalsTab, pendingCount]);

  useEffect(() => {
    if (menuRef.current) {
      (menuRef.current as unknown as Record<string, unknown>).hass = hass;
      (menuRef.current as unknown as Record<string, unknown>).narrow = narrow;
    }
  }, [hass, narrow]);

  const refreshTokens = useCallback(async () => {
    setLoadingTokens(true);
    setTokensError(null);
    try {
      const data = await api.listTokens();
      setTokens(data);
    } catch (e: unknown) {
      setTokensError(e instanceof Error ? e.message : t("shell.loadTokensFailed"));
    } finally {
      setLoadingTokens(false);
    }
  }, []);

  useEffect(() => {
    refreshTokens();
    api.getSettings().then(setSettings).catch(() => null);
  }, [refreshTokens]);

  const refreshPendingCount = useCallback(async () => {
    try {
      const resp = await api.listApprovals({ status: "pending", limit: 1 });
      setPendingCount(resp.total);
      // Seed the baseline on the very first successful fetch so a reload with
      // History persisted and an already-nonzero count never looks like a rise.
      if (lastSeenPendingCountRef.current === null) lastSeenPendingCountRef.current = resp.total;
    } catch {
      // Silent failure: badge just won't update. Don't surface in UI.
    }
  }, []);

  useEffect(() => {
    refreshPendingCount();
    // Poll as a fallback (in case the event subscription below is unavailable or
    // an event is missed); the subscription makes the common case instant.
    const id = setInterval(refreshPendingCount, 5_000);
    return () => clearInterval(id);
  }, [refreshPendingCount]);

  // Instant approval updates: subscribe to the HA event bus so a queued or
  // resolved approval refreshes the badge AND (via approvalSignal) the Approvals
  // list immediately, instead of waiting for the next poll. Falls back silently
  // to polling if the connection has no subscribeEvents.
  useEffect(() => {
    const conn = (hass as {
      connection?: { subscribeEvents?: (cb: () => void, ev: string) => Promise<() => void> };
    } | null)?.connection;
    if (!conn?.subscribeEvents) return;
    let cancelled = false;
    const unsubs: Array<() => void> = [];
    const onApprovalEvent = () => {
      refreshPendingCount();
      setApprovalSignal((n) => n + 1);
    };
    unsubs.push(subscribeApprovalEvents(hass, {
      onClaimChanged: (approvalId, claimed) => setClaimedApprovals((prev) => {
        if (claimed === prev.has(approvalId)) return prev;
        const next = new Set(prev);
        if (claimed) next.add(approvalId); else next.delete(approvalId);
        return next;
      }),
    }));
    for (const ev of ["phoenix_mcp_approval_requested", "phoenix_mcp_approval_resolved"]) {
      conn.subscribeEvents(onApprovalEvent, ev)
        .then((unsub) => { if (cancelled) unsub(); else unsubs.push(unsub); })
        .catch(() => { /* subscription unavailable: polling covers it */ });
    }
    return () => { cancelled = true; unsubs.forEach((u) => u()); };
  }, [hass, refreshPendingCount]);

  const openDetail = useCallback((id: string) => {
    setView({ name: "detail", tokenId: id });
    setTab("tokens");
  }, []);

  const openWizard = useCallback(() => {
    setTab("tokens");
    setView({ name: "wizard" });
  }, []);

  const goBack = useCallback(() => {
    setView({ name: "list" });
    refreshTokens();
  }, [refreshTokens]);

  // Jump to the Settings tab (used by the "Use Agent Chat"/"Setup Agent Chat"
  // options in the create/connect flows). Refresh tokens first so a token just
  // made in the wizard is listed when the user returns to the Tokens tab.
  const goToSettings = useCallback(() => {
    refreshTokens();
    setView({ name: "list" });
    setTab("settings");
  }, [refreshTokens]);

  const onTabClick = useCallback((t: Tab) => {
    // A new approval arrived while History was open: jump to Pending so it's
    // not missed. Gated on a genuine rise (not just "something is pending",
    // which is true often enough that it would otherwise defeat the
    // persisted-sub-tab restore below on almost every visit).
    if (
      t === "approvals" && approvalsTab === "history" &&
      lastSeenPendingCountRef.current !== null && pendingCount > lastSeenPendingCountRef.current
    ) {
      setApprovalsTab("pending");
    }
    setTab(t);
    setView({ name: "list" });
    if (t === "tokens") refreshTokens();
  }, [refreshTokens, approvalsTab, pendingCount]);

  const TABS: Tab[] = ["tokens", "approvals", "changes", "mesa", "audit", "settings"];

  function handleTabKeyDown(e: React.KeyboardEvent) {
    const idx = TABS.indexOf(tab);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft" || e.key === "Home" || e.key === "End") {
      e.preventDefault();
      const tablist = e.currentTarget;
      const next = e.key === "Home"
        ? TABS[0]
        : e.key === "End"
          ? TABS[TABS.length - 1]
          : e.key === "ArrowRight"
            ? TABS[(idx + 1) % TABS.length]
            : TABS[(idx - 1 + TABS.length) % TABS.length];
      onTabClick(next);
      window.requestAnimationFrame(() => {
        const target = tablist.querySelector<HTMLButtonElement>(`#phx-tab-${next}`);
        target?.focus();
      });
    }
  }

  return (
    <div className="phx-shell">
      <h1 className="sr-only">{t("shell.srTitle")}</h1>
      <div className="phx-topbar">
        {narrow && (
          <header className="phx-header">
            <ha-menu-button ref={menuRef as React.RefObject<HTMLElement>} />
            <span className="phx-header-title">Phoenix MCP</span>
          </header>
        )}

        <nav className="phx-tabs" aria-label={t("shell.sectionsAria")}>
        {/* Row split is visual only (CSS order, mobile media query); DOM order
            stays the desktop reading order (tabs, doclink, header actions) so
            keyboard/AT navigation is unaffected. Same divergence pattern as
            .two-col's mobile reorder. */}
        <div role="tablist" aria-label={t("shell.sectionsAria")} onKeyDown={handleTabKeyDown} style={{ display: "contents" }}>
          {TABS.map((tabKey, i) => (
            <React.Fragment key={tabKey}>
              {/* No aria-controls: only the active panel is mounted (per-tab
                  lazy data fetch), so inactive tabs would point at missing ids. */}
              <button
                role="tab"
                id={`phx-tab-${tabKey}`}
                aria-selected={tab === tabKey}
                tabIndex={tab === tabKey ? 0 : -1}
                className={`phx-tab${tab === tabKey ? " active" : ""}${i < 3 ? " phx-tab-row1" : " phx-tab-row2"}`}
                onClick={() => onTabClick(tabKey)}
                aria-label={tabKey === "approvals" && pendingCount > 0
                  ? t("shell.approvalsTabAria", { count: pendingCount })
                  : undefined}
              >
                {t(TAB_LABEL_KEYS[tabKey])}
                {tabKey === "approvals" && pendingCount > 0 && (
                  <span className="phx-tab-badge" aria-hidden="true">{pendingCount}</span>
                )}
              </button>
            </React.Fragment>
          ))}
        </div>

        {/* Forces the row-1/row-2 split at a fixed point (tabs row2 wraps
            below) instead of wherever content happens to overflow; inert
            (zero size, no wrap) outside the mobile layout. */}
        <div className="phx-tabs-break" aria-hidden="true" />

        <a
          className="phx-tab phx-tab-doclink"
          href={DOCS_BASE_URL}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("shell.documentation")}
          <svg className="phx-tab-extlink" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            <polyline points="15 3 21 3 21 9" />
            <line x1="10" y1="14" x2="21" y2="3" />
          </svg>
        </a>

        <div className="phx-tab-spacer" />

        <div className="phx-header-actions">
          {settings?.kill_switch ? (
            <span className="phx-killswitch-banner" role="status" title={t("shell.killSwitchTitle")}>
              <span className="phx-killswitch-text-full">{t("shell.killSwitchFull")}</span>
              <span className="phx-killswitch-text-short">{t("shell.killSwitchShort")}</span>
            </span>
          ) : tokens.length > 0 ? (
            <button className="btn btn-outline btn-sm btn-header-action" onClick={() => openAgentCli()}>
              {t("shell.agentChat")}
            </button>
          ) : null}
        </div>
        </nav>
      </div>

      <main
        className="phx-content"
        id={`phx-tabpanel-${tab}`}
        role="tabpanel"
        aria-labelledby={`phx-tab-${tab}`}
      >
        <h2 className="sr-only">{t(TAB_LABEL_KEYS[tab])}</h2>
        {tab === "tokens" && view.name === "list" && (
          <TokenListView
            tokens={tokens}
            loading={loadingTokens}
            error={tokensError}
            onRefresh={refreshTokens}
            onOpenDetail={openDetail}
            onLaunchWizard={openWizard}
            showCreate={showCreate}
            onOpenCreate={() => setShowCreate(true)}
            onCloseCreate={() => setShowCreate(false)}
            onOpenSettings={goToSettings}
          />
        )}
        {tab === "tokens" && view.name === "wizard" && (
          <OnboardingWizard onCancel={goBack} onFinish={openDetail} />
        )}
        {tab === "tokens" && view.name === "detail" && (
          <TokenDetailView
            tokenId={view.tokenId}
            onBack={goBack}
            onRefresh={refreshTokens}
            presetsEnabled={settings?.token_presets_enabled ?? false}
            esphome={
              settings
                ? {
                    integration: !!settings.esphome_integration,
                    builder: !!settings.esphome_builder,
                  }
                : null
            }
          />
        )}
        {tab === "approvals" && (
          <ApprovalsView
            tab={approvalsTab}
            onTabChange={setApprovalsTab}
            onCountChange={refreshPendingCount}
            refreshSignal={approvalSignal}
            claimedApprovals={claimedApprovals}
            openApprovalId={deepApprovalId}
            onConsumedDeepLink={() => setDeepApprovalId(null)}
          />
        )}
        {tab === "mesa" && <MesaView />}
        {tab === "audit" && <AuditView tokens={tokens} />}
        {tab === "changes" && <ChangesView hass={hass} />}
        {tab === "settings" && (
          <SettingsView
            settings={settings}
            onSettingsChange={setSettings}
            theme={theme}
            onThemeChange={onThemeChange}
            language={language}
            onLanguageChange={onLanguageChange}
          />
        )}
      </main>

      {/* Panel-only window. Not rendered in global-visibility mode (the inject
          module owns the window then), nor while the kill switch is on. When
          global injection is unsupported, __phxAgentChat is absent, so this
          renders as the fallback. */}
      {settings && !settings.kill_switch && agentCliOpen
        && !(settings.agentcli_global && globalChatReady) && (
        <AgentCliWindow
          key={agentCliKey}
          tokens={tokens}
          instances={agentCliInstances}
          scrollbackLines={settings.agentcli_scrollback_lines}
          initialTokenId={tokens[0]?.id ?? ""}
          onClose={closeAgentCli}
          hass={hass}
        />
      )}
    </div>
  );
}

class PhoenixPanelElement extends HTMLElement {
  private _root: Root | null = null;
  private _hass: unknown = null;
  private _narrow: boolean = false;
  private _prevUserId: string | undefined = undefined;
  private _theme: Theme = "auto";
  private _i18nStarted: boolean = false;
  private _language: string = getLanguagePreference();

  connectedCallback() {
    this.style.touchAction = "pan-y";

    let saved: string | null = null;
    try { saved = localStorage.getItem("phx-theme"); } catch { /* storage blocked: use default */ }
    if (saved === "light" || saved === "dark" || saved === "auto") {
      this._theme = saved;
    }
    this._applyThemeClass();

    const shadow = this.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
    shadow.appendChild(style);

    const mount = document.createElement("div");
    mount.style.height = "100%";
    shadow.appendChild(mount);

    this._root = createRoot(mount);
    this._render();
  }

  disconnectedCallback() {
    this._root?.unmount();
    this._root = null;
  }

  set hass(hass: unknown) {
    this._hass = hass;
    setHass(hass);
    if (!this._i18nStarted) {
      this._i18nStarted = true;
      loadTranslations(hass, resolveLanguage(hass)).then(() => this._render());
    } else {
      // Under "auto" the language follows the HA profile, which an admin can
      // change without reloading the page. A no-op unless it actually differs.
      void syncTranslations(hass).then((changed) => { if (changed) this._render(); });
    }
    const uid = (hass as Record<string, Record<string, string>> | null)?.user?.id;
    if (uid !== this._prevUserId) {
      this._prevUserId = uid;
      this._render();
    }
    if (this._theme === "auto") this._applyThemeClass();
  }

  set narrow(value: boolean) {
    if (this._narrow !== value) {
      this._narrow = value;
      this._render();
    }
  }

  private _applyThemeClass() {
    this.classList.remove("phx-theme-light", "phx-theme-dark");
    if (this._theme === "light") {
      this.classList.add("phx-theme-light");
    } else if (this._theme === "dark") {
      this.classList.add("phx-theme-dark");
    } else {
      // Auto: follow HA's dark mode preference when available
      const hassThemes = (this._hass as { themes?: { darkMode?: boolean } } | null)?.themes;
      if (hassThemes?.darkMode === true) {
        this.classList.add("phx-theme-dark");
      } else if (hassThemes?.darkMode === false) {
        this.classList.add("phx-theme-light");
      }
      // If darkMode is undefined, no class - CSS prefers-color-scheme handles it
    }
  }

  private _setLanguage(pref: string) {
    this._language = pref;
    setLanguagePreference(pref);
    // Repaint immediately so the dropdown reflects the choice, then again once
    // the new catalog lands. Without the first render the select would appear
    // stuck on the old value for the length of a round trip.
    this._render();
    loadTranslations(this._hass, resolveLanguage(this._hass)).then(() => this._render());
  }

  private _setTheme(t: Theme) {
    this._theme = t;
    try { localStorage.setItem("phx-theme", t); } catch { /* storage blocked: skip persistence */ }
    this._applyThemeClass();
    // The global Agent Chat window lives outside this element (document.body
    // shadow host) and cannot see our class change; tell it to re-resolve.
    window.dispatchEvent(new CustomEvent("phx-theme-changed"));
    this._render();
  }

  private _render() {
    // Strings arrive over the websocket, so hold the first paint until they do
    // rather than flashing raw keys. Same shape as the existing hass guard.
    if (this._root && this._hass && isI18nReady()) {
      this._root.render(
        <PhoenixApp
          hass={this._hass}
          narrow={this._narrow}
          theme={this._theme}
          onThemeChange={(t) => this._setTheme(t)}
          language={this._language}
          onLanguageChange={(lang) => this._setLanguage(lang)}
        />
      );
    }
  }
}

if (!customElements.get("phoenix-mcp-panel")) {
  customElements.define("phoenix-mcp-panel", PhoenixPanelElement);
}
