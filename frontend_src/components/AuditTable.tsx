import { useState } from "react";
import type { AuditEntry, Outcome } from "../types";
import { Modal } from "./Modal";
import { YamlView, toYaml } from "./YamlView";
import { friendlyResource } from "../utils";
import { localeDateTime, localeDateTimeShort, t } from "../i18n";
import { auditSourceLabel, isNamedAuditSource } from "../utils/audit_source";

interface Props {
  entries: AuditEntry[];
  loading?: boolean;
  // Current token_id -> name, so a renamed token's existing audit rows show its
  // current name. The stored entry.token_name is the fallback for tokens no
  // longer active (archived/revoked) and for admin actions.
  tokenNames?: Record<string, string>;
}

function formatTokenName(name: string): string {
  return name.replace(/^(admin):(.+)$/, "$1 ($2)");
}

function formatTokenNameShort(name: string): string {
  return name.replace(/^(admin):(.{4}).+(.{4})$/, "$1 ($2...$3)");
}

function formatTs(iso: string): string {
  return localeDateTime(iso);
}

function formatTsShort(iso: string): string {
  return localeDateTimeShort(iso);
}

const OUTCOME_LABEL_KEYS: Record<Outcome, string> = {
  allowed: "audit.outcomeAllowed",
  denied: "audit.outcomeDenied",
  not_found: "audit.outcomeNotFound",
  rate_limited: "audit.outcomeRateLimited",
  not_implemented: "audit.outcomeNotImplemented",
  invalid_request: "audit.outcomeInvalidRequest",
  pending_approval: "audit.outcomePendingApproval",
};

/** Outcome label, falling back to the raw backend slug when unmapped. */
function outcomeLabel(outcome: Outcome): string {
  const key = OUTCOME_LABEL_KEYS[outcome];
  return key ? t(key) : outcome;
}

const OUTCOME_CLASS: Record<Outcome, string> = {
  allowed: "outcome-allowed",
  denied: "outcome-denied",
  not_found: "outcome-not_found",
  rate_limited: "outcome-rate_limited",
  not_implemented: "outcome-not_implemented",
  invalid_request: "outcome-invalid_request",
  pending_approval: "outcome-pending_approval",
};

type SortKey = "timestamp" | "token_name" | "method" | "resource" | "outcome" | "client_ip";
type SortDir = "asc" | "desc";

function SortArrow({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  const active = col === sortKey;
  return <span className={`sort-arrow${active ? " active" : ""}`}>{active ? (sortDir === "asc" ? "↑" : "↓") : "↕"}</span>;
}

function DetailRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="detail-row">
      <span className="detail-label">{label}</span>
      <span className={mono ? "detail-value-mono" : "detail-value"}>{value}</span>
    </div>
  );
}

function EntryDetailModal({ entry, tokenName, onClose, onNavigatePrevious, onNavigateNext }: {
  entry: AuditEntry;
  tokenName: string;
  onClose: () => void;
  onNavigatePrevious?: () => void;
  onNavigateNext?: () => void;
}) {
  // Render the recorded payload as YAML in HA's code editor (via YamlView);
  // if it is not valid JSON, show the raw string verbatim.
  const prettyPayload = entry.payload
    ? (() => { try { return toYaml(JSON.parse(entry.payload) as Record<string, unknown>); } catch { return entry.payload; } })()
    : null;
  return (
    <Modal
      titleId="audit-detail-title"
      onClose={onClose}
      onNavigatePrevious={onNavigatePrevious}
      onNavigateNext={onNavigateNext}
      recordNavigation
    >
      <h3 className="modal-title audit-section-title" id="audit-detail-title">{t("audit.detailTitle")}</h3>
      <DetailRow label={t("audit.rowTime")} value={formatTs(entry.timestamp)} />
      <DetailRow
        label={t("audit.rowToken")}
        value={tokenName !== entry.token_name
          ? `${formatTokenName(tokenName)} (${formatTokenName(entry.token_name)})`
          : formatTokenName(tokenName)}
      />
      <DetailRow label={t("audit.rowMode")} value={entry.pass_through ? t("tokens.passThrough") : t("tokens.scoped")} />
      <DetailRow label={t("audit.rowMethod")} value={entry.method} mono />
      <DetailRow label={t("audit.rowResource")} value={friendlyResource(entry.resource)} mono />
      <DetailRow label={t("audit.rowOutcome")} value={outcomeLabel(entry.outcome)} />
      {entry.mesa_advisory && <DetailRow label={t("audit.rowMesa")} value={t("audit.mesaAdvisory")} />}
      <DetailRow
        label={t("audit.rowIp")}
        value={auditSourceLabel(entry.client_ip)}
        mono={!isNamedAuditSource(entry.client_ip)}
      />
      <DetailRow label={t("audit.rowRequestId")} value={entry.request_id} mono />
      {prettyPayload && (
        <div className="audit-payload-section">
          <span className="detail-label">{t("audit.rowPayload")}</span>
          <YamlView value={prettyPayload} />
        </div>
      )}
      <div className="modal-actions">
        <button className="btn btn-text" onClick={onClose}>{t("common.close")}</button>
      </div>
    </Modal>
  );
}

export function AuditTable({ entries, loading, tokenNames }: Props) {
  const [selected, setSelected] = useState<AuditEntry | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("timestamp");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Prefer the token's current name (by id) over the snapshot stored on the row,
  // so a rename is reflected in its historical audit entries.
  const displayName = (e: AuditEntry): string => tokenNames?.[e.token_id] ?? e.token_name;

  if (loading) {
    return <div className="loading-wrap"><div className="spinner" /><span>{t("common.loading")}</span></div>;
  }

  if (entries.length === 0) {
    return <p className="audit-empty">{t("audit.empty")}</p>;
  }

  function handleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("asc"); }
  }

  function ariaSort(key: SortKey): "ascending" | "descending" | "none" {
    if (sortKey !== key) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  }

  const sorted = [...entries].sort((a, b) => {
    const va = sortKey === "token_name"
      ? displayName(a)
      : sortKey === "client_ip" ? auditSourceLabel(a.client_ip) : (a[sortKey] ?? "");
    const vb = sortKey === "token_name"
      ? displayName(b)
      : sortKey === "client_ip" ? auditSourceLabel(b.client_ip) : (b[sortKey] ?? "");
    if (va < vb) return sortDir === "asc" ? -1 : 1;
    if (va > vb) return sortDir === "asc" ? 1 : -1;
    return 0;
  });
  const selectedIndex = selected
    ? sorted.findIndex((entry) => entry.request_id === selected.request_id)
    : -1;

  function th(label: string, key: SortKey) {
    return (
      <th
        className={`sortable${sortKey === key ? " sort-active" : ""}`}
        aria-sort={ariaSort(key)}
      >
        <button type="button" className="table-sort-btn" onClick={() => handleSort(key)}>
          {label}<SortArrow col={key} sortKey={sortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }

  return (
    <div>
      {selected && (
        <EntryDetailModal
          key={selected.request_id}
          entry={selected}
          tokenName={displayName(selected)}
          onClose={() => setSelected(null)}
          onNavigatePrevious={selectedIndex > 0 ? () => setSelected(sorted[selectedIndex - 1]) : undefined}
          onNavigateNext={selectedIndex >= 0 && selectedIndex < sorted.length - 1
            ? () => setSelected(sorted[selectedIndex + 1])
            : undefined}
        />
      )}
      <table className="data-table audit-table">
        <thead>
          <tr>
            {th(t("audit.rowOutcome"), "outcome")}
            {th(t("audit.rowToken"), "token_name")}
            {th(t("audit.rowTime"), "timestamp")}
            {th(t("audit.rowMethod"), "method")}
            {th(t("audit.rowResource"), "resource")}
            {th(t("audit.rowIp"), "client_ip")}
          </tr>
        </thead>
        <tbody>
          {sorted.map((entry) => (
            <tr
              key={entry.request_id}
              className={`clickable${entry.pass_through ? " pass-through-row" : ""}`}
              onClick={() => setSelected(entry)}
            >
              <td>
                <span className={`outcome-badge ${OUTCOME_CLASS[entry.outcome]}`}>
                  {outcomeLabel(entry.outcome)}
                </span>
                {entry.mesa_advisory && (
                  <span className="outcome-badge mesa-advisory-badge" title={t("audit.mesaAdvisory")} aria-label={t("audit.mesaAdvisory")}>MESA</span>
                )}
              </td>
              <td title={formatTokenName(displayName(entry))}>
                {/* The row's own onClick (above) makes the whole row clickable
                    for the mouse; this button keeps it keyboard- and screen-
                    reader-accessible (tab to it, Enter/Space to open the
                    entry). stopPropagation so activating it does not also
                    re-fire the row's handler. Lives in the Token column, not
                    Outcome, because Outcome is one of the columns mobile
                    hides (display:none un-renders anything inside it). */}
                <button
                  type="button"
                  className="row-open row-link-btn"
                  onClick={(e) => { e.stopPropagation(); setSelected(entry); }}
                  aria-label={t("audit.openEntryAria", { outcome: outcomeLabel(entry.outcome), name: displayName(entry) })}
                >
                  {formatTokenNameShort(displayName(entry))}
                </button>
              </td>
              <td>
                <span className="audit-time-full">{formatTs(entry.timestamp)}</span>
                <span className="audit-time-short">{formatTsShort(entry.timestamp)}</span>
              </td>
              <td className="audit-cell-method">{entry.method}</td>
              <td className="audit-cell-resource" title={friendlyResource(entry.resource)}>{friendlyResource(entry.resource)}</td>
              <td
                className={`audit-cell-source${isNamedAuditSource(entry.client_ip) ? " audit-source-name" : ""}`}
                title={auditSourceLabel(entry.client_ip)}
              >
                {auditSourceLabel(entry.client_ip)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
