import { useState, useEffect, useCallback, useMemo } from "react";
import type { AuditEntry, TokenRecord, Outcome } from "../types";
import { api } from "../api";
import { AuditTable } from "../components/AuditTable";
import { RefreshIcon } from "../index";
import { t } from "../i18n";

interface Props {
  tokens: TokenRecord[];
}

type TimeWindow = "" | "5m" | "1h" | "24h" | "1w";

const TIME_WINDOW_MS: Record<Exclude<TimeWindow, "">, number> = {
  "5m": 5 * 60 * 1000,
  "1h": 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "1w": 7 * 24 * 60 * 60 * 1000,
};

const PAGE_SIZE = 100;

export function AuditView({ tokens }: Props) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [outcomeFilter, setOutcomeFilter] = useState<Outcome | "">("");
  const [tokenFilter, setTokenFilter] = useState("");
  const [timeWindow, setTimeWindow] = useState<TimeWindow>("");
  const [methodFilter, setMethodFilter] = useState("");
  const [resourceFilter, setResourceFilter] = useState("");
  const [ipFilter, setIpFilter] = useState("");

  // Method/resource/ip are now server-side filters (they used to filter an
  // already-fetched batch client-side); debounce them so typing doesn't fire a
  // request per keystroke.
  const [debouncedMethod, setDebouncedMethod] = useState("");
  const [debouncedResource, setDebouncedResource] = useState("");
  const [debouncedIp, setDebouncedIp] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedMethod(methodFilter);
      setDebouncedResource(resourceFilter);
      setDebouncedIp(ipFilter);
    }, 300);
    return () => clearTimeout(timer);
  }, [methodFilter, resourceFilter, ipFilter]);

  // Current id -> name map so renamed tokens show their current name on existing
  // audit rows (falls back to the row's stored name for archived/admin entries).
  const tokenNames = useMemo(
    () => Object.fromEntries(tokens.map((t) => [t.id, t.name])),
    [tokens],
  );

  const filterParams = useMemo(() => ({
    outcome: outcomeFilter || undefined,
    token_id: tokenFilter || undefined,
    since: timeWindow ? new Date(Date.now() - TIME_WINDOW_MS[timeWindow]).toISOString() : undefined,
    method: debouncedMethod || undefined,
    resource: debouncedResource || undefined,
    ip: debouncedIp || undefined,
  }), [outcomeFilter, tokenFilter, timeWindow, debouncedMethod, debouncedResource, debouncedIp]);

  const load = useCallback(async (offset: number) => {
    if (offset === 0) setLoading(true); else setLoadingMore(true);
    try {
      const resp = await api.getAudit({ ...filterParams, limit: PAGE_SIZE, offset });
      setEntries((prev) => (offset === 0 ? resp.entries : [...prev, ...resp.entries]));
      setHasMore(offset + resp.entries.length < resp.total);
    } catch {
      if (offset === 0) setEntries([]);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [filterParams]);

  // (Re)load from the top whenever a filter changes.
  useEffect(() => { load(0); }, [load]);

  return (
    <div className="view-root">
      <div className="card">
        <div className="filter-row">
          <select
            aria-label={t("audit.filterOutcomeAria")}
            className="input input-auto"
            value={outcomeFilter}
            onChange={(e) => setOutcomeFilter(e.target.value as Outcome | "")}
          >
            <option value="">{t("audit.allOutcomes")}</option>
            <option value="allowed">{t("audit.outcomeAllowed")}</option>
            <option value="denied">{t("audit.outcomeDenied")}</option>
            <option value="not_found">{t("audit.outcomeNotFound")}</option>
            <option value="rate_limited">{t("audit.outcomeRateLimited")}</option>
            <option value="not_implemented">{t("audit.outcomeNotImplemented")}</option>
            <option value="invalid_request">{t("audit.outcomeInvalidRequest")}</option>
            <option value="pending_approval">{t("audit.outcomePendingApproval")}</option>
          </select>
          <select
            aria-label={t("audit.filterTokenAria")}
            className="input input-auto"
            value={tokenFilter}
            onChange={(e) => setTokenFilter(e.target.value)}
          >
            <option value="">{t("audit.allTokens")}</option>
            {tokens.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
          <select
            aria-label={t("audit.filterTimeAria")}
            className="input input-auto"
            value={timeWindow}
            onChange={(e) => setTimeWindow(e.target.value as TimeWindow)}
          >
            <option value="">{t("audit.allTime")}</option>
            <option value="1w">{t("audit.pastWeek")}</option>
            <option value="24h">{t("audit.past24h")}</option>
            <option value="1h">{t("audit.pastHour")}</option>
            <option value="5m">{t("audit.past5m")}</option>
          </select>
          <input
            className="input"
            aria-label={t("audit.filterMethodAria")}
            placeholder={t("audit.methodPlaceholder")}
            value={methodFilter}
            onChange={(e) => setMethodFilter(e.target.value)}
          />
          <input
            className="input"
            aria-label={t("audit.filterResourceAria")}
            placeholder={t("audit.resourcePlaceholder")}
            value={resourceFilter}
            onChange={(e) => setResourceFilter(e.target.value)}
          />
          {/* Grouped so mobile can put IP and Refresh on one row together
              (see .audit-ip-row) instead of Refresh wrapping off on its own
              below every other filter. */}
          <div className="audit-ip-row">
            <input
              className="input"
              aria-label={t("audit.filterIpAria")}
              placeholder={t("audit.ipPlaceholder")}
              value={ipFilter}
              onChange={(e) => setIpFilter(e.target.value)}
            />
            <button
              className="btn btn-ghost btn-sm btn-icon"
              onClick={() => load(0)}
              title={t("common.refresh")}
              aria-label={t("audit.refreshAria")}
            >
              <RefreshIcon />
            </button>
          </div>
        </div>

        <AuditTable
          entries={entries}
          loading={loading}
          tokenNames={tokenNames}
        />
        {hasMore && (
          <div className="approval-history-more">
            <button className="btn btn-ghost btn-sm" disabled={loadingMore}
              onClick={() => load(entries.length)}>
              {loadingMore ? t("common.loading") : t("common.loadMore")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
