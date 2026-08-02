/** Shared presentational helpers used across panel views and the in-context
 * profile modal. Kept out of index.tsx so reusing them (e.g. ProfileEditor in
 * the injected quick-add modal) does not pull the whole panel app into a chunk.
 */
import { t } from "../i18n";

export function Loading() {
  return (
    <div className="loading-wrap" role="status">
      <div className="spinner" aria-hidden="true" />
      <span>{t("common.loading")}</span>
    </div>
  );
}

export function ErrorMsg({ msg }: { msg: string }) {
  return <div className="banner banner-error" role="alert">{msg}</div>;
}

// The documentation is a hosted site, not a copy shipped inside the integration,
// so every help link leaves Home Assistant. Exported because index.tsx links the
// site root from the tab bar and the two must not drift.
export const DOCS_BASE_URL = "https://leecaochang.github.io/phoenix-mcp/";

// Small "?" badge next to a Settings tab card's label, linking out to the
// matching section of the documentation site.
export function DocsHelpLink({ path, label }: { path: string; label: string }) {
  return (
    <a
      className="docs-help-link"
      href={DOCS_BASE_URL + path}
      target="_blank"
      rel="noopener noreferrer"
      title={t("common.docsTitle")}
      aria-label={t("common.docsAria", { label })}
    >
      ?
    </a>
  );
}

export function RefreshIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ display: "block" }}>
      <polyline points="1 4 1 10 7 10" />
      <polyline points="23 20 23 14 17 14" />
      <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4-4.64 4.36A9 9 0 0 1 3.51 15" />
    </svg>
  );
}

// Export = data leaves the profile store, out to a file (arrow up and out of the tray).
export function ExportIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ display: "block" }}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  );
}

// Import = a file's contents land in the profile store (arrow down into the tray).
export function ImportIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ display: "block" }}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}
