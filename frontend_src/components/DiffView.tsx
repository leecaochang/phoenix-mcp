/** Before/after diff pane for approval review. */
import React, { useMemo, useState } from "react";
import { toYaml, YamlView } from "./YamlView";
import { t } from "../i18n";

// The approval diff carries before/after as JSON strings (possibly truncated).
// Render them as YAML so the line diff reads like the stored config; if a side
// is not valid JSON (e.g. truncated), diff it verbatim.
function asYaml(s: string | null): string {
  if (s == null || s === "") return "";
  try {
    return toYaml(JSON.parse(s) as Record<string, unknown>);
  } catch {
    return s;
  }
}

/** Renders a config (JSON string) as an all-red "removed" pane, for delete
 * approvals where there is no "after". */
export function RemovedPane({ value }: { value: string | null }) {
  const { beforeRows } = diffLines(asYaml(value), "");
  return <RawDiffPane rows={beforeRows} tone="remove" />;
}

// The side-by-side vs stacked choice is a durable preference like the
// Diff|Preview mode: one localStorage key shared by the approval diff and the
// Changes tab. The caller supplies its own fallback for when nothing is
// stored (the Changes tab defaults to stacked on narrow viewports).
const DIFF_LAYOUT_KEY = "phx-diff-layout";

export function storedDiffLayout(fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(DIFF_LAYOUT_KEY);
    return v === null ? fallback : v === "stacked";
  } catch {
    return fallback;
  }
}

export function rememberDiffLayout(stacked: boolean): void {
  try {
    localStorage.setItem(DIFF_LAYOUT_KEY, stacked ? "stacked" : "side");
  } catch {
    // Storage blocked: the choice still applies for this view, just not durably.
  }
}

// Rendering the panes through HA's own <ha-code-editor> (syntax highlighting and
// a line-number gutter, the editor an operator already knows from the ESPHome
// add-on and the Audit tab) rather than the plain diff <pre>. Durable like the
// layout choice, and a separate key so the two are independent.
const CODE_VIEW_KEY = "phx-diff-code-view";

export function storedCodeView(): boolean {
  try {
    return localStorage.getItem(CODE_VIEW_KEY) === "code";
  } catch {
    return false;
  }
}

export function rememberCodeView(code: boolean): void {
  try {
    localStorage.setItem(CODE_VIEW_KEY, code ? "code" : "plain");
  } catch {
    // Storage blocked: the choice still applies for this view, just not durably.
  }
}

/** Icon button switching the panes between the plain line diff and HA's code
 * editor. Deliberately an icon toggle rather than a "Diff | Code" segmented
 * control: the dashboard surfaces already put a "Diff | Preview" control in this
 * same toolbar, and two segments both labelled Diff read as one broken control.
 * This toggles HOW the panes render, not WHICH content they show, so it composes
 * with that one instead of competing with it. */
export function CodeToggle({ code, onToggle }: { code: boolean; onToggle: () => void }) {
  const label = code ? t("changes.toCodeOff") : t("changes.toCodeOn");
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm btn-icon diff-code-toggle"
      onClick={onToggle}
      aria-label={label}
      aria-pressed={code}
      title={label}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
           strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
           style={{ display: "block" }}>
        <path d="M5.5 4.5 2 8l3.5 3.5" />
        <path d="M10.5 4.5 14 8l-3.5 3.5" />
      </svg>
    </button>
  );
}

/** Icon button toggling a before/after diff between side-by-side and stacked.
 * The icon shows the layout you will switch TO. Shared by the approval diff and
 * the Changes tab. */
export function LayoutToggle({ stacked, onToggle }: { stacked: boolean; onToggle: () => void }) {
  const label = stacked ? t("changes.toSideBySide") : t("changes.toStacked");
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm btn-icon diff-layout-toggle"
      onClick={onToggle}
      aria-label={label}
      title={label}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" style={{ display: "block" }}>
        {stacked ? (
          // Target = side-by-side: two vertical panes.
          <>
            <rect x="2" y="2.5" width="5" height="11" rx="1" />
            <rect x="9" y="2.5" width="5" height="11" rx="1" />
          </>
        ) : (
          // Target = stacked: two horizontal panes.
          <>
            <rect x="2.5" y="2" width="11" height="5" rx="1" />
            <rect x="2.5" y="9" width="11" height="5" rx="1" />
          </>
        )}
      </svg>
    </button>
  );
}

export function BeforeAfter({ before, after, toolbarExtra }: {
  before: string | null;
  after: string | null;
  /** Optional extra control rendered in the toolbar, just before the layout
   * toggle (the approval card slots its Diff|Preview switch here). */
  toolbarExtra?: React.ReactNode;
}) {
  const [stacked, setStacked] = useState(() => storedDiffLayout(false));
  const toggleStacked = () => {
    const next = !stacked;
    rememberDiffLayout(next);
    setStacked(next);
  };
  const [code, setCode] = useState(() => storedCodeView());
  const toggleCode = () => {
    const next = !code;
    rememberCodeView(next);
    setCode(next);
  };
  const beforeYaml = asYaml(before);
  const afterYaml = asYaml(after);
  const { beforeRows, afterRows } = useMemo(
    () => diffLines(beforeYaml, afterYaml),
    [beforeYaml, afterYaml],
  );
  return (
    <div className="change-diff-wrap">
      <div className="change-diff-toolbar">
        <span className="change-diff-hint">
          {code
            // Say what this view GIVES UP. The tinting is how an approver sees
            // what they are approving, and a syntax-coloured pane looks
            // authoritative enough that its absence would otherwise go unnoticed.
            ? t("changes.hintCode")
            : t("changes.hintDiff")}
        </span>
        {toolbarExtra}
        <CodeToggle code={code} onToggle={toggleCode} />
        <LayoutToggle stacked={stacked} onToggle={toggleStacked} />
      </div>
      <div className={`yaml-diff-cols${stacked ? " stacked" : ""}`}>
        <div className="yaml-diff-col">
          <div className="yaml-pane-head"><span className="approval-diff-label">{t("common.before")}</span></div>
          {code ? <YamlView value={beforeYaml} /> : <RawDiffPane rows={beforeRows} tone="remove" />}
        </div>
        <div className="yaml-diff-col">
          <div className="yaml-pane-head"><span className="approval-diff-label">{t("common.after")}</span></div>
          {code ? <YamlView value={afterYaml} /> : <RawDiffPane rows={afterRows} tone="add" />}
        </div>
      </div>
    </div>
  );
}

export interface DiffLine {
  text: string;
  changed: boolean;
}

// Above this combined line count we skip the O(m*n) LCS and render both sides
// plainly (no per-line highlight), so a large raw file cannot stall the panel.
const MAX_DIFF_LINES = 3000;

/** Line-level diff of two text blocks via LCS, returning each side's lines with a
 * `changed` flag (a removed line on the before side, an added line on the after
 * side). Used for raw file / configuration.yaml version snapshots. */
export function diffLines(before: string, after: string): { beforeRows: DiffLine[]; afterRows: DiffLine[] } {
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
  if (a.length + b.length > MAX_DIFF_LINES) {
    return {
      beforeRows: a.map((text) => ({ text, changed: false })),
      afterRows: b.map((text) => ({ text, changed: false })),
    };
  }
  const m = a.length;
  const n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const beforeRows: DiffLine[] = [];
  const afterRows: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      beforeRows.push({ text: a[i], changed: false });
      afterRows.push({ text: b[j], changed: false });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      beforeRows.push({ text: a[i], changed: true });
      i++;
    } else {
      afterRows.push({ text: b[j], changed: true });
      j++;
    }
  }
  while (i < m) beforeRows.push({ text: a[i++], changed: true });
  while (j < n) afterRows.push({ text: b[j++], changed: true });
  return { beforeRows, afterRows };
}

/** Renders one side of a raw-text line diff; changed lines are tinted by tone. */
export function RawDiffPane({ rows, tone }: { rows: DiffLine[]; tone: "remove" | "add" }) {
  if (rows.length === 0) return <pre className="yaml-pre yaml-pre-empty">{t("common.empty")}</pre>;
  return (
    <pre className="yaml-pre raw-diff">
      {rows.map((r, idx) => (
        <div key={idx} className={r.changed ? `diff-line diff-${tone}` : "diff-line"}>
          {r.text === "" ? " " : r.text}
        </div>
      ))}
    </pre>
  );
}
