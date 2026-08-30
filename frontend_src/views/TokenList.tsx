import { useState, useEffect, useCallback } from "react";
import type { TokenRecord, ArchivedTokenRecord } from "../types";
import { TokenCreateModal } from "../components/TokenCreateModal";
import { ArchivedTokenTable } from "../components/ArchivedTokenTable";
import { api } from "../api";
import { Loading, ErrorMsg, RefreshIcon } from "../index";
import { formatDate, tokenStatus, tokenStatusLabel } from "../utils";
import { compareStrings, localeDateTime, t } from "../i18n";

const MAX_ACTIVE_TOKENS_WARNING = 50;

function relativeTime(iso: string | null): string {
  if (!iso) return t("common.never");
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return t("tokens.justNow");
  const m = Math.floor(s / 60);
  if (m < 60) return t("tokens.minutesAgo", { m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("tokens.hoursAgo", { h });
  return t("tokens.daysAgo", { d: Math.floor(h / 24) });
}


type SortKey = "name" | "mode" | "status" | "created" | "updated" | "expires" | "last_used";
type SortDir = "asc" | "desc";

function SortArrow({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  const active = col === sortKey;
  return <span className={`sort-arrow${active ? " active" : ""}`}>{active ? (sortDir === "asc" ? "↑" : "↓") : "↕"}</span>;
}

interface Props {
  tokens: TokenRecord[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onOpenDetail: (id: string) => void;
  onLaunchWizard: () => void;
  showCreate: boolean;
  onOpenCreate: () => void;
  onCloseCreate: () => void;
  onOpenSettings: () => void;
}

export function TokenListView({ tokens, loading, error, onRefresh, onOpenDetail, onLaunchWizard, showCreate, onOpenCreate, onCloseCreate, onOpenSettings }: Props) {
  const [filter, setFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [archived, setArchived] = useState<ArchivedTokenRecord[] | null>(null);
  const [archivedLoading, setArchivedLoading] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const refreshArchived = useCallback(async () => {
    setArchivedLoading(true);
    try {
      setArchived(await api.listArchivedTokens());
    } catch {
      setArchived([]);
    } finally {
      setArchivedLoading(false);
    }
  }, []);

  useEffect(() => {
    if (showArchived) refreshArchived();
  }, [showArchived, refreshArchived]);

  function handleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("asc"); }
  }

  function ariaSort(key: SortKey): "ascending" | "descending" | "none" {
    if (sortKey !== key) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  }

  const filtered = tokens.filter((t) => {
    const q = filter.toLowerCase();
    if (!q) return true;
    const status = tokenStatus(t);
    return [t.name, status, tokenStatusLabel(status)]
      .some((value) => value.toLowerCase().includes(q));
  });

  const sorted = [...filtered].sort((a, b) => {
    let va: string | number = "";
    let vb: string | number = "";
    switch (sortKey) {
      case "name":     va = a.name.toLowerCase();   vb = b.name.toLowerCase(); break;
      case "mode":     va = a.pass_through ? "1" : "0"; vb = b.pass_through ? "1" : "0"; break;
      case "status":   va = tokenStatusLabel(tokenStatus(a)); vb = tokenStatusLabel(tokenStatus(b)); break;
      case "created":  va = a.created_at ?? "";      vb = b.created_at ?? ""; break;
      case "updated":  va = a.updated_at ?? "";      vb = b.updated_at ?? ""; break;
      case "expires":  va = a.expires_at ?? "9999";  vb = b.expires_at ?? "9999"; break;
      case "last_used": va = a.last_used_at ?? "";   vb = b.last_used_at ?? ""; break;
    }
    const comparison = sortKey === "status"
      ? compareStrings(String(va), String(vb))
      : va < vb ? -1 : va > vb ? 1 : 0;
    return sortDir === "asc" ? comparison : -comparison;
  });

  function handleCreated(record: TokenRecord) {
    onRefresh();
    onCloseCreate();
    onOpenDetail(record.id);
  }

  function handleArchivedDeleted(id: string) {
    setArchived((prev) => prev?.filter((t) => t.id !== id) ?? null);
  }

  function th(label: string, key: SortKey, className?: string) {
    return (
      <th
        className={`sortable${sortKey === key ? " sort-active" : ""}${className ? ` ${className}` : ""}`}
        aria-sort={ariaSort(key)}
      >
        <button type="button" className="table-sort-btn" onClick={() => handleSort(key)}>
          {label}<SortArrow col={key} sortKey={sortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }

  // First run: no tokens and no filter. Show an onboarding hero that launches
  // the wizard instead of the empty table. The create modal is still mounted
  // here so the header "Create Token" button works in the empty state.
  if (!loading && !error && tokens.length === 0 && !filter && !showArchived) {
    return (
      <div className="view-root">
        <div className="card wizard-hero">
          <div className="wizard-hero-title">{t("tokens.heroTitle")}</div>
          <p className="wizard-hero-sub">
            {t("tokens.heroSub")}
          </p>
          <div className="wizard-hero-actions">
            <button className="btn btn-primary" onClick={onLaunchWizard}>{t("tokens.setUpFirstAgent")}</button>
          </div>
        </div>
        {showCreate && (
          <TokenCreateModal
            existingNames={tokens.map((t) => t.name)}
            onCreated={handleCreated}
            onClose={onCloseCreate}
            onOpenSettings={onOpenSettings}
          />
        )}
      </div>
    );
  }

  return (
    <div className="view-root">
      {tokens.length >= MAX_ACTIVE_TOKENS_WARNING && (
        <div className="banner banner-warn">
          {t("tokens.maxWarning", { max: MAX_ACTIVE_TOKENS_WARNING })}
        </div>
      )}

      <div className="card">
        <div className="filter-row">
          <input
            className="input"
            aria-label={t("tokens.filterAria")}
            placeholder={t("tokens.filterPlaceholder")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div className="filter-row-right">
            <button
              className="btn btn-ghost btn-sm"
              onClick={onLaunchWizard}
            >
              {t("tokens.setUpAgent")}
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setShowArchived((v) => !v)}
            >
              {showArchived ? t("tokens.hideArchived") : t("tokens.showArchived")}
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={onOpenCreate}
            >
              {t("tokens.createToken")}
            </button>
          </div>
          {/* Own flex item (not nested in filter-row-right) so mobile CSS can
              pull it up next to the filter input via order, without changing
              desktop's DOM/tab order (same trick as the tab bar's row split). */}
          <button
            className="btn btn-ghost btn-sm btn-icon filter-row-refresh"
            onClick={onRefresh}
            title={t("common.refresh")}
            aria-label={t("tokens.refreshAria")}
          >
            <RefreshIcon />
          </button>
        </div>

        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorMsg msg={error} />
        ) : (
          <table className="data-table token-table">
            <thead>
              <tr>
                {th(t("tokens.colName"), "name")}
                {th(t("tokens.colMode"), "mode")}
                {th(t("tokens.colStatus"), "status")}
                {th(t("tokens.colCreated"), "created")}
                {th(t("tokens.colUpdated"), "updated")}
                {th(t("tokens.colExpires"), "expires")}
                {th(t("tokens.colLastUsed"), "last_used")}
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 && (
                <tr>
                  <td colSpan={7} className="token-table-empty">
                    {filter ? t("tokens.emptyFiltered") : t("tokens.empty")}
                  </td>
                </tr>
              )}
              {sorted.map((tok) => {
                const status = tokenStatus(tok);
                const statusClass = status === "Active" ? "badge-green" : status === "Expired" ? "badge-grey" : "badge-red";
                return (
                  <tr
                    key={tok.id}
                    className={`clickable${tok.pass_through ? " pass-through-row" : ""}`}
                    onClick={() => onOpenDetail(tok.id)}
                  >
                    <td className="token-name">
                      {/* The row's own onClick (above) makes the whole row
                          clickable for the mouse; this button keeps it
                          keyboard- and screen-reader-accessible (tab to it,
                          Enter/Space to open the token). stopPropagation so
                          activating it does not also re-fire the row's
                          handler. */}
                      <button
                        type="button"
                        className="row-open"
                        onClick={(e) => { e.stopPropagation(); onOpenDetail(tok.id); }}
                        aria-label={t("tokens.editAria", { name: tok.name })}
                      >
                        {tok.name}
                      </button>
                    </td>
                    <td>
                      {tok.pass_through
                        ? <span className="badge badge-amber">{t("tokens.passThrough")}</span>
                        : <span className="badge badge-blue">{t("tokens.scoped")}</span>}
                    </td>
                    <td><span className={`badge ${statusClass}`}>{tokenStatusLabel(status)}</span></td>
                    <td title={tok.created_at ? localeDateTime(tok.created_at) : undefined}>{formatDate(tok.created_at)}</td>
                    <td title={tok.updated_at ? localeDateTime(tok.updated_at) : undefined}>{relativeTime(tok.updated_at)}</td>
                    <td>{formatDate(tok.expires_at)}</td>
                    <td>{relativeTime(tok.last_used_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {showArchived && (
        <div className="card">
          <div className="card-header">
            <h3 className="card-header-title">{t("tokens.archivedTitle")}</h3>
          </div>
          {archivedLoading ? (
            <Loading />
          ) : (
            <ArchivedTokenTable
              tokens={archived ?? []}
              onDeleted={handleArchivedDeleted}
            />
          )}
        </div>
      )}

      {showCreate && (
        <TokenCreateModal
          existingNames={tokens.map((t) => t.name)}
          onCreated={handleCreated}
          onClose={onCloseCreate}
          onOpenSettings={onOpenSettings}
        />
      )}
    </div>
  );
}
