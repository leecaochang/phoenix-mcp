/** Live Lovelace layout preview for the dashboard diff surfaces (approval
 * review and the Changes tab). Opportunistically reuses HA's <hui-card>
 * element the way YamlView reuses <ha-code-editor>: if the element is not
 * registered in this browser session (HA lazy-loads it with the first
 * dashboard view), callers hide the Preview toggle and the UI stays the plain
 * text diff. Cards are rendered inert: entity states are live, but taps,
 * keyboard focus, and tap actions are all neutralized so an admin cannot
 * actuate a device from a preview. */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { currentHass } from "../api";
import { copyToClipboard } from "../utils";
import { t } from "../i18n";

export type PreviewCardEntry =
  | { kind: "card"; config: Record<string, unknown> }
  | { kind: "invalid" };

export interface PreviewSection {
  title: string | null;
  cards: PreviewCardEntry[];
}

export interface PreviewView {
  title: string;
  kind: "cards" | "strategy" | "invalid" | "empty";
  sections: PreviewSection[];
}

function isDict(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function cardEntries(cards: unknown): PreviewCardEntry[] {
  if (!Array.isArray(cards)) return [];
  return cards.map((c) => (isDict(c) ? { kind: "card" as const, config: c } : { kind: "invalid" as const }));
}

// Home Assistant's card components style themselves from these CSS custom
// properties. Phoenix MCP's panel is a separate shadow-rooted custom element, not a
// descendant of the live Lovelace view, so it never receives them: the panel
// only defines its own --phx-* tokens. Worse, the panel's :host rule sets a
// real (inherited) `color` for its own chrome, which otherwise wins over
// anything HA sets further up the tree for any card content that inherits
// color rather than setting its own. applyLiveHaTheme forwards the resolved
// values from the live HA page onto the preview container, and pins `color`
// back to HA's own text color for that subtree specifically.
const HA_THEME_VARS = [
  "--primary-color", "--accent-color", "--dark-primary-color", "--light-primary-color",
  "--text-primary-color", "--primary-background-color", "--secondary-background-color",
  "--primary-text-color", "--secondary-text-color", "--disabled-text-color",
  "--divider-color", "--outline-color",
  "--card-background-color", "--ha-card-background", "--ha-card-border-color",
  "--ha-card-border-width", "--ha-card-border-radius", "--ha-card-box-shadow",
  "--state-icon-color", "--state-icon-active-color", "--paper-item-icon-color",
  "--paper-item-icon-active-color", "--label-badge-background-color", "--label-badge-text-color",
  "--error-color", "--warning-color", "--success-color", "--info-color",
  "--rgb-primary-color", "--rgb-accent-color", "--rgb-card-background-color",
  "--rgb-primary-text-color", "--rgb-secondary-text-color", "--rgb-state-icon-active-color",
  "--rgb-disabled-color", "--mdc-theme-primary", "--mdc-theme-secondary",
  "--mdc-theme-surface", "--mdc-theme-on-primary", "--mdc-theme-on-surface",
  "--switch-checked-color", "--switch-unchecked-color",
];

/** Copies Home Assistant's live, already-resolved theme variables onto
 * `el` so nested <hui-card> content renders in the admin's real dashboard
 * theme, not Phoenix MCP's own palette or an unstyled fallback. HA stamps every
 * theme override (custom themes AND default-theme dark mode) as inline
 * custom properties on document.documentElement (themes-mixin ->
 * applyThemesOnElement), so those are copied wholesale for full custom-theme
 * fidelity; the curated list backfills the stylesheet-defined defaults that
 * are never stamped inline. color-scheme is pinned from hass.themes.darkMode
 * (the authoritative mode signal; HA itself only reflects it in a meta tag),
 * so native controls and any light-dark()-valued variables resolve to HA's
 * mode, not Phoenix MCP's. Known gap: this is the admin's ACTIVE global theme; a
 * dashboard or view configured with its own distinct theme override is not
 * specifically resolved (would need the target's own theme name applied via
 * hass.themes, a larger, more version-sensitive change) - not attempted. */
function applyLiveHaTheme(el: HTMLElement, hass: unknown): void {
  try {
    const html = document.documentElement;
    const computed = getComputedStyle(html);
    for (const name of HA_THEME_VARS) {
      const value = computed.getPropertyValue(name).trim();
      if (value) el.style.setProperty(name, value);
    }
    const inline = html.style;
    for (let i = 0; i < inline.length; i++) {
      const prop = inline[i];
      if (prop.startsWith("--")) el.style.setProperty(prop, inline.getPropertyValue(prop));
    }
    const darkMode = (hass as { themes?: { darkMode?: boolean } } | null)?.themes?.darkMode;
    el.style.setProperty(
      "color-scheme",
      darkMode === true ? "dark" : darkMode === false ? "light" : "unset",
    );
    // Real (non-custom) properties: override the panel's own :host color so
    // inherited-color card content follows HA's text color, not Phoenix MCP's.
    el.style.color = "var(--primary-text-color, inherit)";
    el.style.backgroundColor = "transparent";
  } catch {
    // Best-effort cosmetic forwarding; a failure here must never break the
    // preview itself, just leave it looking less correct.
  }
}

/** Walks a rendered card's light DOM and open shadow roots collecting the
 * error text of every <hui-error-card> HA swapped in for a card (or card row)
 * whose config failed. The message lives on the error card's config (error +
 * optional message fields); the rendered text is the fallback. Depth-capped:
 * a pathological tree must not hang the panel. Exported for tests. */
export function collectConfigErrors(root: Element): string[] {
  const out: string[] = [];
  const walk = (el: Element, depth: number): void => {
    if (depth > 10) return;
    if (el.localName === "hui-error-card") {
      const cfg = (el as unknown as { _config?: { error?: unknown; message?: unknown } })._config;
      const parts = [cfg?.error, cfg?.message]
        .filter((p): p is string => typeof p === "string" && p.length > 0);
      out.push(
        parts.join(" - ")
        || el.shadowRoot?.textContent?.trim()
        || el.textContent?.trim()
        || t("changes.previewErrorNoDetail"),
      );
    }
    if (el.shadowRoot) {
      for (const kid of Array.from(el.shadowRoot.children)) walk(kid, depth + 1);
    }
    for (const kid of Array.from(el.children)) walk(kid, depth + 1);
  };
  walk(root, 0);
  return out;
}

/** Wraps one card config object into a one-card dashboard config previewable
 * by collectPreviewViews/DashboardPreview; null for anything but a dict. The
 * approval card feeds an add/edit's After side from the approval's own
 * args.card, which is always the full object (diff strings can truncate). */
export function wrapCardPreviewConfig(card: unknown): Record<string, unknown> | null {
  return isDict(card) ? { views: [{ cards: [card] }] } : null;
}

/** Same, from a card-tool diff side's JSON string (diff.before/diff.after).
 * Null input, unparseable JSON (a card large enough to hit the diff's
 * truncation bound), or a non-object result all return null, so callers treat
 * it exactly like "not previewable". */
export function singleCardPreviewConfig(cardJson: string | null | undefined): Record<string, unknown> | null {
  if (!cardJson) return null;
  try {
    return wrapCardPreviewConfig(JSON.parse(cardJson));
  } catch {
    return null;
  }
}

/** Normalize an agent-proposed (possibly garbage) dashboard config into
 * renderable views. Total: never throws. Returns null when the config has no
 * previewable shape at all (not a dict, no views list, or a strategy
 * dashboard whose views only exist at runtime); callers use null to hide the
 * Preview toggle entirely. */
export function collectPreviewViews(config: unknown): PreviewView[] | null {
  if (!isDict(config)) return null;
  if ("strategy" in config) return null;
  const views = config.views;
  if (!Array.isArray(views)) return null;
  return views.map((view, idx) => {
    const fallback = t("changes.previewViewFallback", { n: idx + 1 });
    if (!isDict(view)) return { title: fallback, kind: "invalid" as const, sections: [] };
    const title =
      (typeof view.title === "string" && view.title) ||
      (typeof view.path === "string" && view.path) ||
      fallback;
    if ("strategy" in view) return { title, kind: "strategy" as const, sections: [] };
    const sections: PreviewSection[] = [];
    const plain = cardEntries(view.cards);
    if (plain.length > 0) sections.push({ title: null, cards: plain });
    if (Array.isArray(view.sections)) {
      for (const section of view.sections) {
        if (!isDict(section)) continue;
        const cards = cardEntries(section.cards);
        if (cards.length > 0) {
          sections.push({ title: typeof section.title === "string" && section.title ? section.title : null, cards });
        }
      }
    }
    if (sections.length === 0) return { title, kind: "empty" as const, sections: [] };
    return { title, kind: "cards" as const, sections };
  });
}

/** True once HA has registered <hui-card>, upgrading in place if that happens
 * after mount (same pattern as YamlView's ha-code-editor handling). */
export function useHuiCardReady(): boolean {
  const [ready, setReady] = useState(() => typeof customElements !== "undefined" && !!customElements.get("hui-card"));
  useEffect(() => {
    if (ready || typeof customElements === "undefined") return;
    let cancelled = false;
    customElements
      .whenDefined("hui-card")
      .then(() => { if (!cancelled) setReady(true); })
      .catch(() => { /* never defined: callers keep the text diff */ });
    return () => { cancelled = true; };
  }, [ready]);
  return ready;
}

// The Diff|Preview choice is a durable preference, not per-record state: an
// operator who prefers the visual preview wants it every time, on both diff
// surfaces (approval card and Changes tab), across reloads. One shared key,
// same localStorage pattern as the panel theme. The Before/After SIDE is
// deliberately not remembered (it is contextual: delete only has a Before,
// add only an After).
const PREVIEW_MODE_KEY = "phx-dash-preview-mode";

export type DiffPreviewMode = "diff" | "preview";

export function storedPreviewMode(): DiffPreviewMode {
  try {
    return localStorage.getItem(PREVIEW_MODE_KEY) === "preview" ? "preview" : "diff";
  } catch {
    return "diff";
  }
}

export function rememberPreviewMode(mode: DiffPreviewMode): void {
  try {
    localStorage.setItem(PREVIEW_MODE_KEY, mode);
  } catch {
    // Storage blocked: the choice still applies for this record, just not durably.
  }
}

/** Small segmented control, shared by the Diff|Preview mode switch and the
 * Changes tab's Before|After side switch. */
export function SegmentedToggle<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: { value: T; label: string; disabled?: boolean }[];
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div className="diff-mode-toggle" role="group" aria-label={ariaLabel}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={`diff-mode-btn${opt.value === value ? " active" : ""}`}
          aria-pressed={opt.value === value}
          disabled={opt.disabled}
          onClick={() => opt.value !== value && onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

/** Renders one card through HA's <hui-card>. Properties are assigned
 * imperatively (hass, preview, config are Lit reactive properties; setting
 * config triggers the card build, and hui-card converts a broken or unknown
 * card type into its own error card internally). Any synchronous failure
 * degrades this one tile to a note. After the build, the tile scans for the
 * error cards HA swapped in and reports their messages up, so the parent can
 * show them as selectable text: inside the inert region they render fine but
 * cannot be selected or copied. Checks run once after paint plus twice
 * delayed, because an uninstalled custom card only becomes an error card
 * after HA's own registration timeout. */
function PreviewCardTile({ hass, config, errorKey, onErrors }: {
  hass: unknown;
  config: Record<string, unknown>;
  errorKey: string;
  onErrors: (key: string, label: string, messages: string[]) => void;
}) {
  const ref = useRef<HTMLElement | null>(null);
  const [failed, setFailed] = useState(false);
  const label = typeof config.type === "string" && config.type ? config.type : t("changes.previewCardFallbackLabel");
  useEffect(() => {
    const el = ref.current as unknown as Record<string, unknown> | null;
    if (!el) return;
    try {
      el.hass = hass;
      el.preview = true;
      el.config = config;
    } catch {
      setFailed(true);
    }
  }, [hass, config]);
  useEffect(() => {
    if (failed) return undefined;
    let cancelled = false;
    const check = () => {
      if (cancelled || !ref.current) return;
      onErrors(errorKey, label, collectConfigErrors(ref.current));
    };
    const raf = requestAnimationFrame(check);
    const t1 = window.setTimeout(check, 400);
    const t2 = window.setTimeout(check, 2500);
    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      onErrors(errorKey, label, []);
    };
  }, [hass, config, failed, errorKey, label, onErrors]);
  if (failed) {
    return <div className="dash-preview-card-error">{t("changes.previewCardFailed")}</div>;
  }
  return <hui-card ref={ref as React.RefObject<HTMLElement>} />;
}

/** Small copy control for an extracted error message; reuses the shared
 * clipboard helper (which has the plain-http fallback this LAN UI needs). */
function CopyErrorButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await copyToClipboard(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Leave the button as-is; the text stays selectable by hand.
    }
  };
  return (
    <button type="button" className="btn btn-ghost btn-sm" onClick={copy}>
      {copied ? t("common.copied") : t("common.copy")}
    </button>
  );
}

/** Catches any render error inside the preview so a bad config or an HA
 * frontend change can never take down the surrounding modal; the Diff mode
 * stays reachable because the mode toggle lives outside this boundary. */
class PreviewErrorBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed) {
      return <p className="dash-preview-note">{t("changes.previewUnavailableLayout")}</p>;
    }
    return this.props.children;
  }
}

export function DashboardPreview({ config, onConfigErrors }: {
  config: unknown;
  /** Called with the current list of card configuration errors whenever it
   * changes, so a host surface can act on them (the approval modal offers
   * "Reject with error message"). */
  onConfigErrors?: (messages: string[]) => void;
}) {
  return (
    <PreviewErrorBoundary>
      <DashboardPreviewInner config={config} onConfigErrors={onConfigErrors} />
    </PreviewErrorBoundary>
  );
}

function DashboardPreviewInner({ config, onConfigErrors }: {
  config: unknown;
  onConfigErrors?: (messages: string[]) => void;
}) {
  const ready = useHuiCardReady();
  const views = useMemo(() => collectPreviewViews(config), [config]);
  // One fresh read per mount: the hass prop drilled through React can be
  // stale (the panel only re-renders on user change), the module singleton
  // is updated on every hass assignment from HA.
  const [hass] = useState<unknown>(() => currentHass());
  const [active, setActive] = useState(0);
  // Config errors reported by the rendered tiles, keyed per visible tile.
  // Rendered OUTSIDE the inert region so the text is selectable/copyable
  // (the whole point: paste the message back to the agent).
  const [cardErrors, setCardErrors] = useState<Record<string, { label: string; messages: string[] }>>({});
  const inertRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => { setActive(0); }, [config]);
  useEffect(() => { setCardErrors({}); }, [config, active]);
  const reportErrors = useCallback((key: string, label: string, messages: string[]) => {
    setCardErrors((prev) => {
      const cur = prev[key];
      if ((cur?.messages ?? []).join("\n") === messages.join("\n")) return prev;
      const next = { ...prev };
      if (messages.length === 0) delete next[key];
      else next[key] = { label, messages };
      return next;
    });
  }, []);
  // Depends on `ready`, not []: when hui-card is not yet registered at mount,
  // the early return below never attaches rootRef, so an effect that only
  // ran once at mount would fire against a null ref and never run again once
  // whenDefined resolves and the real content (with the ref) finally renders.
  useEffect(() => {
    if (rootRef.current) applyLiveHaTheme(rootRef.current, hass);
  }, [ready, hass]);
  useEffect(() => {
    if (!onConfigErrors) return;
    onConfigErrors(Object.values(cardErrors).flatMap((entry) => entry.messages));
  }, [cardErrors, onConfigErrors]);
  // React 18 JSX has no inert attribute handling; set the property directly.
  // This is the primary interaction kill switch (pointer events, focus, and
  // tap actions all die with it); the CSS pointer-events:none on
  // .dash-preview-cards and the capture-phase swallow below are backstops.
  //
  // Deliberately NO dependency array, unlike its two neighbours above: it must
  // re-assert inert on every render, because the node it targets is replaced
  // whenever the previewed card set changes. An empty array would set inert
  // once on a node that is later swapped out, leaving a live, clickable card
  // preview, which is the one thing this component must never produce.
  useEffect(() => {
    const node = inertRef.current as (HTMLDivElement & { inert?: boolean }) | null;
    if (node) node.inert = true;
  });

  if (!ready) {
    return (
      <p className="dash-preview-note">{t("changes.previewNotReady")}</p>
    );
  }
  if (views === null) return <p className="dash-preview-note">{t("changes.previewNoLayout")}</p>;
  if (views.length === 0) return <p className="dash-preview-note">{t("changes.previewNoViews")}</p>;

  const view = views[Math.min(active, views.length - 1)];

  const swallow = (e: React.SyntheticEvent) => {
    const cards = inertRef.current;
    if (cards && e.target instanceof Node && cards.contains(e.target) && e.target !== cards) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    <div className="dash-preview" ref={rootRef}>
      {views.length > 1 && (
        <div className="dash-preview-tabs" role="group" aria-label={t("changes.previewViewsAria")}>
          {views.map((v, idx) => (
            <button
              key={idx}
              type="button"
              aria-pressed={idx === active}
              className={`approval-detail-tab${idx === active ? " active" : ""}`}
              onClick={() => setActive(idx)}
            >
              {v.title}
            </button>
          ))}
        </div>
      )}
      {view.kind === "strategy" && (
        <p className="dash-preview-note">{t("changes.previewStrategyView")}</p>
      )}
      {view.kind === "invalid" && (
        <p className="dash-preview-note">{t("changes.previewBadView")}</p>
      )}
      {view.kind === "empty" && <p className="dash-preview-note">{t("changes.previewNoCards")}</p>}
      {view.kind === "cards" && (
        <>
          <span className="dash-preview-disclaimer">{t("changes.previewDisclaimer")}</span>
          <div
            className="dash-preview-scroll"
            onClickCapture={swallow}
            onKeyDownCapture={swallow}
            onPointerDownCapture={swallow}
          >
            <div className="dash-preview-cards" ref={inertRef}>
              {view.sections.map((section, sIdx) => (
                <React.Fragment key={sIdx}>
                  {section.title && <div className="dash-preview-section-title">{section.title}</div>}
                  <div className="dash-preview-grid">
                    {section.cards.map((card, cIdx) =>
                      card.kind === "card"
                        ? (
                          <PreviewCardTile
                            key={cIdx}
                            hass={hass}
                            config={card.config}
                            errorKey={`${sIdx}.${cIdx}`}
                            onErrors={reportErrors}
                          />
                        )
                        : (
                          <div key={cIdx} className="dash-preview-card-error">
                            {t("changes.previewBadCard")}
                          </div>
                        ))}
                  </div>
                </React.Fragment>
              ))}
            </div>
          </div>
          {Object.keys(cardErrors).length > 0 && (
            <div className="dash-preview-error-panel">
              <div className="dash-preview-error-head">{t("changes.previewErrorHead")}</div>
              {Object.entries(cardErrors).map(([key, entry]) =>
                entry.messages.map((msg, mIdx) => (
                  <div key={`${key}.${mIdx}`} className="dash-preview-error-item">
                    <div className="dash-preview-error-meta">
                      <code>{entry.label}</code>
                      <CopyErrorButton text={msg} />
                    </div>
                    <pre className="dash-preview-error-msg">{msg}</pre>
                  </div>
                )))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
