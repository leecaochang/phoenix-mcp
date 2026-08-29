import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApprovalDiff, ApprovalRecord, ApprovalStatus, BatchApproveResult } from "../types";
import { api } from "../api";
import { Loading, ErrorMsg } from "../index";
import { Modal } from "../components/Modal";
import { BeforeAfter, RemovedPane } from "../components/DiffView";
import { collectPreviewViews, DashboardPreview, rememberPreviewMode, SegmentedToggle, singleCardPreviewConfig, storedPreviewMode, useHuiCardReady, wrapCardPreviewConfig } from "../components/DashboardPreview";
import { YamlView, toYaml } from "../components/YamlView";
import { approvalStatusLabel, formatDateTime, friendlyToolName } from "../utils";
import { clearReasonDraft, getReasonDraft, setReasonDraft } from "../utils/approval_reason_draft";
import { localizedApprovalReason } from "../utils/approval_reason";
import { friendlyApprovalSummary, rememberApprovalView, storedApprovalView, type ApprovalView } from "../utils/approval_summary";
import { useLatestRequest } from "../utils/latest_request";
import {
  notifyAgentChatReviewClosed,
  notifyAgentChatReviewDecided,
} from "../utils/agentchat_review";
import { hasMessage, t, tn } from "../i18n";

interface Props {
  /** Which sub-tab is active. Owned by the parent so it survives this view
   *  unmounting (switching to another top-level tab and back) and is
   *  persisted across reloads. */
  tab: "pending" | "history";
  /** Called to change the sub-tab (clicks, keyboard nav, or the deep-link effect). */
  onTabChange: (tab: "pending" | "history") => void;
  /** Called when an approval resolves so the parent can refresh the badge count. */
  onCountChange?: () => void;
  /** Bumped by the parent on each HA approval event; when it changes, the current
   *  list reloads immediately (instant update instead of waiting for the poll). */
  refreshSignal?: number;
  /** Approvals whose saved action is executing right now, from the HA claim event.
    *  Approve runs its tool inline in the admin's request, so nothing resolves for
    *  seconds; these render non-actionable for that window so a second click, which
    *  the server would only answer with a 409, is not offered in the first place. */
  claimedApprovals?: ReadonlySet<string>;
  /** Deep-link target from a notification (/phoenix-mcp/approvals/{id}); opens that approval. */
  openApprovalId?: string | null;
  /** Called once the deep-link has been consumed so the parent can clear it. */
  onConsumedDeepLink?: () => void;
}

const POLL_INTERVAL_MS = 10_000;
const HISTORY_PAGE = 50;
const HISTORY_FILTERS: (ApprovalStatus | "all")[] = ["all", "approved", "failed", "rejected", "expired", "cancelled"];
const FILTER_LABEL_KEYS: Record<string, string> = {
  all: "approvals.filterAll",
  approved: "approvals.filterApproved",
  failed: "approvals.reason.execution_failed",
  rejected: "approvals.filterRejected",
  expired: "approvals.filterExpired",
  cancelled: "approvals.filterCancelled",
};

/** The approval's summary in the operator's language.
 *
 *  The backend stores the English sentence (it doubles as the audit trail) and,
 *  since the diff-summary work, the catalog key and params that produced it.
 *  Falls back to the stored English for records written before that, and for a
 *  key this bundle does not know, which is what a newer backend behind a cached
 *  panel looks like. */
export function diffSummary(diff: ApprovalDiff | undefined): string {
  if (diff?.summary_key && hasMessage(diff.summary_key)) {
    return t(diff.summary_key, diff.summary_params);
  }
  return diff?.summary ?? "";
}

/** Preserve a useful stored title only for records too old to contain any
 * deterministic friendly context. Current records always use the resolver. */
function approvalListTitle(diff: ApprovalDiff | undefined, view: ApprovalView): string {
  const friendly = friendlyApprovalSummary(diff).title;
  if (view === "details") return diffSummary(diff) || friendly;
  const unknown = t("approvalSummary.fallback.unknown.title");
  return friendly === unknown && diffSummary(diff) ? diffSummary(diff) : friendly;
}

const FRIENDLY_DETAIL_MAX_CHARS = 280;

function boundedFriendlyDetail(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  if (compact.length <= FRIENDLY_DETAIL_MAX_CHARS) return compact;
  return `${compact.slice(0, FRIENDLY_DETAIL_MAX_CHARS - 3).trimEnd()}...`;
}

function structuredErrorText(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  for (const key of ["error", "message", "reason"]) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate;
    if (candidate && typeof candidate === "object") {
      const nested = (candidate as Record<string, unknown>).message;
      if (typeof nested === "string" && nested.trim()) return nested;
    }
  }
  return null;
}

/** Pull one concise tool error out of a resolved approval's saved result.
 *
 * Older integration failures stored a JSON document containing the short
 * `error` plus complete MESA explanations. Summary must extract the sentence,
 * never render that diagnostic document as prose. Details retains it verbatim.
 */
export function extractResultErrorText(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const toolResult = (result as Record<string, unknown>).tool_result;
  if (!toolResult || typeof toolResult !== "object") return null;
  const direct = structuredErrorText(toolResult);
  if (direct) return boundedFriendlyDetail(direct);
  const content = (toolResult as Record<string, unknown>).content;
  if (!Array.isArray(content)) return null;
  for (const item of content) {
    const text = item && typeof item === "object" ? (item as Record<string, unknown>).text : null;
    if (typeof text !== "string" || !text.trim()) continue;
    try {
      const parsed = JSON.parse(text);
      const structured = structuredErrorText(parsed);
      if (structured) return boundedFriendlyDetail(structured);
    } catch {
      // Plain-text executor errors are the normal shape.
    }
    return boundedFriendlyDetail(text);
  }
  return null;
}

const LOCALIZED_EXECUTOR_ERRORS: Readonly<Record<string, string>> = {
  "The integration's state, resource membership, permissions, or effective MESA profile changed after approval. Review it again.":
    "approvalSummary.history.error.integrationChanged",
  "The integration logger set, override, or visibility changed after approval. Review it again.":
    "approvalSummary.history.error.integrationLoggerChanged",
  "Disabled integrations cannot be reloaded.":
    "approvalSummary.history.error.integrationDisabled",
  "This integration does not currently support reload.":
    "approvalSummary.history.error.integrationReloadUnsupported",
  "Integration not found.":
    "approvalSummary.history.error.integrationNotFound",
  "Failed to reload integration.":
    "approvalSummary.history.error.integrationReloadFailed",
};

const INTEGRATION_STATE_NOT_RELOADABLE = /^Integration state (.+) is not reloadable\.$/;

/** Keep Summary fully localized; raw diagnostics remain available in Details. */
export function localizedResultErrorText(result: unknown): string | null {
  const detail = extractResultErrorText(result);
  if (!detail) return null;
  const key = LOCALIZED_EXECUTOR_ERRORS[detail];
  if (key) return t(key);
  const stateMatch = detail.match(INTEGRATION_STATE_NOT_RELOADABLE);
  if (stateMatch) {
    return t("approvalSummary.history.error.integrationStateNotReloadable", {
      state: stateMatch[1],
    });
  }
  return t("approvalSummary.history.error.generic");
}

/** Readable rejection/cancellation reason: slug -> label, and for an
 *  execution_failed the underlying tool error (e.g. "Forbidden.", a validation
 *  message) instead of the bare slug. Free-text admin reasons pass through. */
export function friendlyReason(record: ApprovalRecord): string {
  const reason = record.rejected_reason;
  if (!reason) return "";
  if (reason === "execution_failed") {
    const detail = localizedResultErrorText(record.result);
    if (detail) return t("approvals.reasonExecutionFailedDetail", { detail });
  }
  return localizedApprovalReason(reason);
}

export function ApprovalsView({ tab, onTabChange, onCountChange, refreshSignal = 0, claimedApprovals, openApprovalId, onConsumedDeepLink }: Props) {
  // Two sources, deliberately: the live claim event (instant, but only for a
  // panel that was open when it fired) and the row's own in_progress from the
  // API (covers a page loaded or reloaded mid-execution).
  const isClaimed = (r: ApprovalRecord) => Boolean(r.in_progress) || Boolean(claimedApprovals?.has(r.id));
  const [records, setRecords] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ApprovalRecord | null>(null);
  // Which approval the open modal was reached from a notification deep-link FOR,
  // rather than a boolean beside `selected`: two states would have to be kept in
  // sync by every open path, and comparing ids cannot desync. An operator who
  // opened the modal from the list already has the queue and its batch controls
  // on screen behind it, so the "others are waiting" banner would repeat what
  // they can see, and a signal that is always present is one that gets skipped.
  const [deepLinkedId, setDeepLinkedId] = useState<string | null>(null);
  const consumedDeepLink = useRef<string | null>(null);
  // Pre-slice total from the pending fetch, not shown.length: the list pages at
  // HISTORY_PAGE, so counting the loaded rows would under-report the queue to an
  // operator deciding whether it is worth opening.
  const [pendingTotal, setPendingTotal] = useState(0);
  const [histFilter, setHistFilter] = useState<ApprovalStatus | "all">("all");
  const [search, setSearch] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [rawOffset, setRawOffset] = useState(0);  // raw records fetched (drives pagination)
  // Batch approve: which pending approvals are ticked, and the last run's result.
  // Ids rather than records, so a refresh that replaces the record objects does
  // not silently drop a tick; ids that vanish are filtered at use, not here.
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchApproveResult | null>(null);
  const [defaultView, setDefaultView] = useState<ApprovalView>(() => storedApprovalView());

  const changeDefaultView = (view: ApprovalView) => {
    setDefaultView(view);
    rememberApprovalView(view);
  };

  // Closing forgets the deep-link, so reopening the SAME approval from the list
  // is treated as what it is: an operator who is looking at the queue.
  const closeRecord = useCallback(() => {
    setDeepLinkedId(null);
    setSelected(null);
  }, []);

  // The two loaders below write the same records/offset/hasMore state and are
  // called from six places with no ordering between them (mount, poll, event,
  // manual refresh, tab or filter change, Load more), so one generation covers
  // both: a response that a newer request has superseded is dropped instead of
  // putting the previous filter's rows back on screen.
  const beginLoad = useLatestRequest();

  const loadPending = useCallback(async (offset = 0) => {
    const isLatest = beginLoad();
    setError(null);
    try {
      const resp = await api.listApprovals({ status: "pending", limit: HISTORY_PAGE, offset });
      if (!isLatest()) return;
      setRecords((prev) => (offset === 0 ? resp.approvals : [...prev, ...resp.approvals]));
      setRawOffset(offset + resp.approvals.length);
      setPendingTotal(resp.total);
      setHasMore(offset + resp.approvals.length < resp.total);
    } catch (e: unknown) {
      if (!isLatest()) return;
      setError(e instanceof Error ? e.message : t("approvals.loadFailed"));
    } finally {
      if (isLatest()) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [beginLoad]);

  const loadHistory = useCallback(async (offset: number) => {
    const isLatest = beginLoad();
    setError(null);
    try {
      const resp = await api.listApprovals({
        status: histFilter === "all" ? undefined : histFilter,
        limit: HISTORY_PAGE,
        offset,
      });
      if (!isLatest()) return;
      const page = histFilter === "all"
        ? resp.approvals.filter((r) => r.status !== "pending")
        : resp.approvals;
      setRecords((prev) => {
        if (offset === 0) return page;
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...page.filter((r) => !seen.has(r.id))];
      });
      setRawOffset(offset + resp.approvals.length);
      setHasMore(offset + resp.approvals.length < resp.total);
    } catch (e: unknown) {
      if (!isLatest()) return;
      setError(e instanceof Error ? e.message : t("approvals.loadFailed"));
    } finally {
      if (isLatest()) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [histFilter, beginLoad]);

  // (Re)load when the tab or the history filter changes.
  useEffect(() => {
    setLoading(true);
    setRecords([]);
    if (tab === "pending") loadPending();
    else loadHistory(0);
  }, [tab, histFilter, loadPending, loadHistory]);

  // Poll while the pending tab is open (fallback; the event signal below makes
  // the common case instant).
  useEffect(() => {
    if (tab !== "pending") return;
    const id = setInterval(loadPending, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [tab, loadPending]);

  // Instant refresh: the parent bumps refreshSignal on each HA approval event
  // (requested/resolved). Reload the current tab the moment it changes, so the
  // list updates in lockstep with the badge instead of on its own poll.
  const lastSignal = useRef(refreshSignal);
  useEffect(() => {
    if (refreshSignal === lastSignal.current) return;
    lastSignal.current = refreshSignal;
    if (tab === "pending") loadPending();
    else loadHistory(0);
  }, [refreshSignal, tab, loadPending, loadHistory]);

  // Reconcile the open detail modal on each approval event. If the modal shows a
  // pending approval and it gets resolved elsewhere (the inline Approve/Reject in
  // Agent Chat, another admin, or expiry), the modal would otherwise sit stale
  // still offering Approve/Reject for an approval that no longer exists. Re-check
  // the open record and close it once it is no longer pending (or is gone).
  const reconcileSignal = useRef(refreshSignal);
  useEffect(() => {
    if (refreshSignal === reconcileSignal.current) return;
    reconcileSignal.current = refreshSignal;
    if (!selected || selected.status !== "pending") return;
    let cancelled = false;
    api.getApproval(selected.id)
      .then((rec) => {
        if (cancelled) return;
        if (rec.status !== "pending") {
          notifyAgentChatReviewDecided(selected.id);
          closeRecord();
          onCountChange?.();
        }
      })
      .catch(() => {
        if (cancelled) return;
        notifyAgentChatReviewDecided(selected.id);
        closeRecord();
      });  // gone/expired: close
    return () => { cancelled = true; };
  }, [refreshSignal, selected, onCountChange]);

  // Fetch a notification deep-link, but do not clear its URL yet. Home Assistant
  // can replace this custom-panel element while routing; keeping the target in
  // the address bar lets the replacement mount finish the same request.
  useEffect(() => {
    if (!openApprovalId) {
      consumedDeepLink.current = null;
      return;
    }
    let cancelled = false;
    api.getApproval(openApprovalId)
      .then((rec) => {
        if (cancelled) return;
        onTabChange(rec.status === "pending" ? "pending" : "history");
        setDeepLinkedId(rec.id);
        setSelected(rec);
      })
      .catch(() => {
        if (cancelled) return;
        notifyAgentChatReviewClosed(openApprovalId);
        // A stale/unknown id cannot be opened, so it is safe to discard.
        consumedDeepLink.current = openApprovalId;
        onConsumedDeepLink?.();
      });
    return () => { cancelled = true; };
  }, [openApprovalId, onConsumedDeepLink, onTabChange]);

  // Clear the route only after React has committed the requested record as the
  // open modal. This prevents the first navigation from being consumed by the
  // panel mount itself, which previously made both chat and notification links
  // require a second click.
  useEffect(() => {
    if (!openApprovalId
      || selected?.id !== openApprovalId
      || deepLinkedId !== openApprovalId
      || consumedDeepLink.current === openApprovalId) return;
    consumedDeepLink.current = openApprovalId;
    onConsumedDeepLink?.();
  }, [deepLinkedId, onConsumedDeepLink, openApprovalId, selected]);

  function handleResolved(updated: ApprovalRecord) {
    closeRecord();
    setRecords((prev) => prev.filter((r) => r.id !== updated.id));
    if (tab === "pending") loadPending();
    onCountChange?.();
  }

  function dismissRecord() {
    if (selected) notifyAgentChatReviewClosed(selected.id);
    closeRecord();
  }

  function switchTopTab(next: "pending" | "history", tablist?: EventTarget & HTMLDivElement) {
    onTabChange(next);
    window.requestAnimationFrame(() => {
      tablist?.querySelector<HTMLButtonElement>(`#approval-tab-${next}`)?.focus();
    });
  }

  function handleTopTabKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const next = e.key === "Home"
      ? "pending"
      : e.key === "End"
        ? "history"
        : tab === "pending" ? "history" : "pending";
    switchTopTab(next, e.currentTarget);
  }

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q || tab === "pending") return records;
    return records.filter((r) =>
      `${r.token_name} ${r.tool_name} ${friendlyToolName(r.tool_name)} ${friendlyApprovalSummary(r.diff).title} ${diffSummary(r.diff)} ${r.rejected_reason ?? ""}`.toLowerCase().includes(q),
    );
  }, [records, search, tab]);

  const selectedIndex = selected ? shown.findIndex((record) => record.id === selected.id) : -1;
  const navigateSelected = useCallback((offset: -1 | 1) => {
    if (!selected) return;
    const index = shown.findIndex((record) => record.id === selected.id);
    const next = shown[index + offset];
    if (!next) return;
    setDeepLinkedId(null);
    setSelected(next);
  }, [selected, shown]);

  // An approval already being executed cannot be batched: the server would answer
  // 409 on its claim and halt the run on something the operator never chose.
  const selectableIds = useMemo(
    () => (tab === "pending" ? shown.filter((r) => !isClaimed(r)).map((r) => r.id) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shown, tab, claimedApprovals],
  );
  const pickedCount = selectableIds.filter((id) => picked.has(id)).length;
  const allPicked = selectableIds.length > 0 && pickedCount === selectableIds.length;
  // Batching one thing is meaningless, so BOTH the bar and the per-row tick boxes
  // appear only from two selectable rows up. Showing a lone tick box with no
  // button to press reads as a broken control (reported).
  const batchable = selectableIds.length > 1;

  // The result banner describes ONE completed run. Clear it as soon as the queue
  // moves underneath it, otherwise it sits above a list it no longer describes
  // and an operator reads a stale "Approved 2" over two freshly-arrived rows
  // (reported). The first render after a result captures the list it belongs to;
  // any change to that list retires it.
  const listKey = records.map((r) => r.id).join(",");
  const bannerListKey = useRef<string | null>(null);
  useEffect(() => {
    if (!batchResult) {
      bannerListKey.current = null;
    } else if (bannerListKey.current === null) {
      bannerListKey.current = listKey;
    } else if (bannerListKey.current !== listKey) {
      setBatchResult(null);
      bannerListKey.current = null;
    }
  }, [batchResult, listKey]);

  const togglePick = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  const toggleAll = () => setPicked(allPicked ? new Set() : new Set(selectableIds));

  async function runBatchApprove() {
    // Send in the order shown, so "stopped at" names the row the operator can see.
    const ids = selectableIds.filter((id) => picked.has(id));
    if (ids.length === 0) return;
    setBatchBusy(true);
    setBatchResult(null);
    try {
      const result = await api.batchApproveApprovals(ids);
      setBatchResult(result);
      // Keep only what was left untouched ticked, so a second click retries
      // exactly the remainder once the operator has dealt with the cause.
      setPicked(new Set(result.remaining));
      await loadPending(0);
      onCountChange?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBatchBusy(false);
    }
  }

  if (loading) return <Loading />;

  return (
    <div className="approvals-view">
      <div className="approvals-toolbar">
      <div className="approvals-tabs" role="tablist" aria-label={t("approvals.tablist")} onKeyDown={handleTopTabKeyDown}>
        {/* No aria-controls on these two: only the active panel is mounted. */}
        <button
          id="approval-tab-pending"
          role="tab"
          aria-selected={tab === "pending"}
          tabIndex={tab === "pending" ? 0 : -1}
          className={`approvals-tab${tab === "pending" ? " active" : ""}`}
          onClick={() => switchTopTab("pending")}
        >
          {tab === "pending" && records.length > 0
            ? t("approvals.tabPendingCount", { count: records.length })
            : t("approvals.tabPending")}
        </button>
        <button
          id="approval-tab-history"
          role="tab"
          aria-selected={tab === "history"}
          tabIndex={tab === "history" ? 0 : -1}
          className={`approvals-tab${tab === "history" ? " active" : ""}`}
          onClick={() => switchTopTab("history")}
        >
          {t("approvals.tabHistory")}
        </button>
      </div>
      <SegmentedToggle
        value={defaultView}
        options={[
          { value: "summary", label: t("approvalSummary.view.summary") },
          { value: "details", label: t("approvalSummary.view.details") },
        ]}
        onChange={changeDefaultView}
        ariaLabel={t("approvalSummary.view.defaultAria")}
      />
      </div>

      <div
        id={`approval-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`approval-tab-${tab}`}
      >
      {tab === "history" && (
        <div className="mesa-controls">
          <div className="mesa-summary" role="group" aria-label={t("approvals.filterGroup")}>
            {HISTORY_FILTERS.map((f) => (
              <button key={f}
                className={`mesa-chip${histFilter === f ? " mesa-chip-active" : ""}`}
                aria-pressed={histFilter === f}
                onClick={() => setHistFilter(f)}>
                {t(FILTER_LABEL_KEYS[f])}
              </button>
            ))}
          </div>
          <input className="input mesa-search" placeholder={t("approvals.searchPlaceholder")}
            value={search} onChange={(e) => setSearch(e.target.value)} aria-label={t("approvals.searchAria")} />
        </div>
      )}

      {error && <ErrorMsg msg={error} />}

      {shown.length === 0 && !error && (
        <div className="approvals-empty">
          {tab === "pending"
            ? t("approvals.emptyPending")
            : search.trim() ? t("approvals.emptySearch") : t("approvals.emptyHistory")}
        </div>
      )}

      {tab === "pending" ? (
        <>
          {batchResult && <BatchResultMsg result={batchResult} onDismiss={() => setBatchResult(null)} />}
          {batchable && (
            <div className="approvals-batch-bar">
              <label className="approvals-batch-all">
                <input
                  type="checkbox"
                  checked={allPicked}
                  ref={(el) => { if (el) el.indeterminate = pickedCount > 0 && !allPicked; }}
                  onChange={toggleAll}
                />
                <span>{t("approvals.selectAll")}</span>
              </label>
              <button
                type="button"
                className="btn btn-primary"
                disabled={pickedCount === 0 || batchBusy}
                onClick={runBatchApprove}
              >
                {batchBusy ? t("approvals.batchRunning") : t("approvals.approveSelected", { count: pickedCount })}
              </button>
            </div>
          )}
          <div className="approvals-list">
            {shown.map((r) => (
              <ApprovalCard
                key={r.id}
                record={r}
                view={defaultView}
                claimed={isClaimed(r)}
                checked={picked.has(r.id)}
                selectable={!isClaimed(r)}
                onToggle={batchable ? () => togglePick(r.id) : undefined}
                onClick={() => setSelected(r)}
              />
            ))}
          </div>
        </>
      ) : (
        shown.length > 0 && (
          <div className="card approval-history">
            {shown.map((r) => (
              <HistoryRow key={r.id} record={r} view={defaultView} onClick={() => setSelected(r)} />
            ))}
          </div>
        )
      )}

      {hasMore && !search.trim() && (
        <div className="approval-history-more">
          <button className="btn btn-ghost btn-sm" disabled={loadingMore}
            onClick={() => { setLoadingMore(true); if (tab === "pending") loadPending(rawOffset); else loadHistory(rawOffset); }}>
            {loadingMore ? t("common.loading") : t("common.loadMore")}
          </button>
        </div>
      )}

      {selected && (
        <ApprovalDetailModal
          key={selected.id}
          record={selected}
          defaultView={defaultView}
          claimed={isClaimed(selected)}
          onClose={dismissRecord}
          onNavigatePrevious={selectedIndex > 0 ? () => navigateSelected(-1) : undefined}
          onNavigateNext={selectedIndex >= 0 && selectedIndex < shown.length - 1 ? () => navigateSelected(1) : undefined}
          onResolved={handleResolved}
          // Only from a notification, and only for a pending record: that is the
          // path on which the queue is invisible. Gating on `tab === "pending"`
          // also keeps pendingTotal fresh, since it is only written by the
          // pending fetch and a deep-link onto an already-resolved record lands
          // on History with a stale count.
          othersPending={
            deepLinkedId === selected.id && tab === "pending" && selected.status === "pending"
              ? Math.max(0, pendingTotal - 1)
              : 0
          }
          onReviewAll={dismissRecord}
        />
      )}
      </div>
    </div>
  );
}

function HistoryRow({ record, view, onClick }: { record: ApprovalRecord; view: ApprovalView; onClick: () => void }) {
  const note = approvalListTitle(record.diff, view);
  return (
    <button type="button" className="approval-history-row" onClick={onClick}>
      <StatusBadge status={record.status} />
      <code className="approval-history-tool">{friendlyToolName(record.tool_name)}</code>
      <span className="approval-history-token">{record.token_name}</span>
      <span className="approval-history-note">{note}</span>
      <span className="approval-history-time">{formatDateTime(record.resolved_at || record.created_at)}</span>
    </button>
  );
}

function StatusBadge({ status }: { status: ApprovalStatus }) {
  const labelKeys: Record<ApprovalStatus, string> = {
    pending: "approvals.statusPending",
    approved: "approvals.statusApproved",
    failed: "approvals.reason.execution_failed",
    rejected: "approvals.statusRejected",
    expired: "approvals.statusExpired",
    cancelled: "approvals.statusCancelled",
  };
  const cls: Record<ApprovalStatus, string> = {
    pending: "badge-amber",
    approved: "badge-green",
    failed: "badge-red",
    rejected: "badge-red",
    expired: "badge-grey",
    cancelled: "badge-grey",
  };
  return <span className={`badge ${cls[status]}`}>{t(labelKeys[status])}</span>;
}

/** Reports what a batch run did. Deliberately three separate facts.
 *
 * "Approved 6" alone would read as a success on a run that stopped at item 7,
 * and "failed" alone would hide the six that really did apply. The remaining
 * count is the one an operator acts on: those are untouched and still pending,
 * not lost.
 */
function BatchResultMsg({ result, onDismiss }: { result: BatchApproveResult; onDismiss: () => void }) {
  const failed = result.failed;
  return (
    <div className={`approvals-batch-result ${failed ? "is-error" : "is-ok"}`} role="status">
      <div className="approvals-batch-result-text">
        <span>{t("approvals.batchApplied", { count: result.applied.length })}</span>
        {failed && (
          <span>
            {t("approvals.batchStopped", {
              tool: friendlyToolName(failed.tool_name ?? ""),
              reason: failed.message || failed.error,
            })}
          </span>
        )}
        {result.remaining.length > 0 && (
          <span>{t("approvals.batchRemaining", { count: result.remaining.length })}</span>
        )}
      </div>
      <button type="button" className="btn btn-ghost" onClick={onDismiss}>{t("common.close")}</button>
    </div>
  );
}

function ApprovalCard({ record, view, claimed, checked, selectable, onToggle, onClick }: {
  record: ApprovalRecord;
  view: ApprovalView;
  claimed?: boolean;
  checked?: boolean;
  selectable?: boolean;
  onToggle?: () => void;
  onClick: () => void;
}) {
  const expiresIn = useExpiresLabel(record.expires_at, record.status);
  const card = (
    <button type="button" className="approval-card" onClick={onClick}>
      <div className="approval-card-header">
        <div className="approval-card-title">
          <span className="approval-card-token">{record.token_name}</span>
          <span className="approval-card-tool">{friendlyToolName(record.tool_name)}</span>
        </div>
        <div className="approval-card-meta">
          {claimed ? <span className="badge badge-info">{t("approvals.processing")}</span> : <StatusBadge status={record.status} />}
          <span className="approval-card-time">
            {formatDateTime(record.created_at)} {expiresIn ? t("approvals.cardTimeSuffix", { expires: expiresIn }) : ""}
          </span>
        </div>
      </div>
      <div className="approval-card-summary">
        {approvalListTitle(record.diff, view)}
      </div>
      {record.rejected_reason && (
        <div className="approval-card-reason">{t("approvals.reasonPrefix", { reason: friendlyReason(record) })}</div>
      )}
    </button>
  );
  if (!onToggle) return card;
  // The checkbox is a SIBLING of the card, never inside it: a checkbox nested in
  // a <button> is invalid, and clicking it would also open the detail modal.
  return (
    <div className="approval-card-row">
      <input
        type="checkbox"
        className="approval-card-check"
        checked={Boolean(checked)}
        disabled={!selectable}
        onChange={onToggle}
        aria-label={t("approvals.selectOne", { tool: friendlyToolName(record.tool_name) })}
      />
      {card}
    </div>
  );
}

function useExpiresLabel(expiresAt: string, status: ApprovalStatus): string | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (status !== "pending") return;
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, [status]);
  if (status !== "pending") return null;
  const expiresMs = Date.parse(expiresAt);
  if (Number.isNaN(expiresMs)) return null;
  const remaining = expiresMs - now;
  if (remaining <= 0) return t("approvals.expiresExpired");
  const mins = Math.round(remaining / 60_000);
  if (mins < 60) return t("approvals.expiresMinutes", { mins });
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return t("approvals.expiresHours", { hours, mins: remMins });
}

interface DetailProps {
  record: ApprovalRecord;
  /** Per-browser default captured when this modal opens. */
  defaultView: ApprovalView;
  /** Its saved action is executing (claimed by an admin's Approve, possibly in
   *  another surface). Locks the actions exactly like this modal's own busy. */
  claimed?: boolean;
  onClose: () => void;
  onResolved: (updated: ApprovalRecord) => void;
  onNavigatePrevious?: () => void;
  onNavigateNext?: () => void;
  /** How many OTHER approvals are waiting, when this modal was reached from a
   *  notification. Zero everywhere else, which hides the banner entirely. */
  othersPending?: number;
  /** Leave this one approval and show the whole queue. */
  onReviewAll?: () => void;
}

function approvalOutcome(record: ApprovalRecord): string {
  if (record.rejected_reason === "execution_interrupted") {
    return t("approvalSummary.history.interrupted.body");
  }
  if (record.rejected_reason === "execution_failed") {
    return t("approvalSummary.history.failed.body", {
      error: localizedResultErrorText(record.result) || friendlyReason(record),
    });
  }
  if (record.status === "approved") return t("approvalSummary.history.approved.body");
  if (record.status === "rejected") {
    return record.rejected_reason
      ? t("approvalSummary.history.rejectedReason.body", { reason: boundedFriendlyDetail(friendlyReason(record)) })
      : t("approvalSummary.history.rejected.body");
  }
  if (record.status === "failed") {
    return t("approvalSummary.history.failed.body", {
      error: localizedResultErrorText(record.result) || friendlyReason(record),
    });
  }
  if (record.status === "expired") return t("approvalSummary.history.expired.body");
  return record.rejected_reason
    ? t("approvalSummary.history.cancelledReason.body", { reason: boundedFriendlyDetail(friendlyReason(record)) })
    : t("approvalSummary.history.cancelled.body");
}

function ApprovalDetailModal({ record, defaultView, claimed, onClose, onResolved, onNavigatePrevious, onNavigateNext, othersPending = 0, onReviewAll }: DetailProps) {
  const [view, setView] = useState<ApprovalView>(defaultView);
  const [activeTab, setActiveTab] = useState<"diff" | "args" | "result">("diff");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Seeded from (and mirrored to) the shared draft store, so the reason typed
  // here is also what the Agent Chat window's Reject button sends for this same
  // approval, and reopening the modal keeps what was typed.
  const [reason, setReason] = useState(() => getReasonDraft(record.id));
  // Card configuration errors the live preview detected for THIS record,
  // powering the one-click "Reject with error message". Last-known-non-empty:
  // flipping back to the Diff view unmounts the preview (whose tiles report
  // empty on cleanup), but the detected errors are still true statements
  // about the proposed card, so the button stays until the record changes.
  const [previewErrors, setPreviewErrors] = useState<string[]>([]);
  const [summaryRejectOpen, setSummaryRejectOpen] = useState(false);
  const summaryReasonRef = useRef<HTMLInputElement>(null);
  useEffect(() => { setPreviewErrors([]); setReason(getReasonDraft(record.id)); }, [record.id]);
  useEffect(() => {
    if (summaryRejectOpen) summaryReasonRef.current?.focus();
  }, [summaryRejectOpen]);
  const onConfigErrors = useCallback((messages: string[]) => {
    if (messages.length > 0) setPreviewErrors(messages);
  }, []);

  const isPending = record.status === "pending";
  const summary = friendlyApprovalSummary(record.diff);
  const detailTabs: Array<"diff" | "args" | "result"> = isPending ? ["diff", "args"] : ["diff", "args", "result"];

  function switchDetailTab(next: "diff" | "args" | "result", tablist?: EventTarget & HTMLDivElement) {
    setActiveTab(next);
    window.requestAnimationFrame(() => {
      tablist?.querySelector<HTMLButtonElement>(`#approval-detail-tab-${next}`)?.focus();
    });
  }

  function handleDetailTabKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const i = detailTabs.indexOf(activeTab);
    const next = e.key === "Home"
      ? detailTabs[0]
      : e.key === "End"
        ? detailTabs[detailTabs.length - 1]
        : e.key === "ArrowRight"
          ? detailTabs[(i + 1) % detailTabs.length]
          : detailTabs[(i - 1 + detailTabs.length) % detailTabs.length];
    switchDetailTab(next, e.currentTarget);
  }

  // Actions are locked by this modal's own in-flight request OR by a claim taken
  // anywhere else. Close is deliberately NOT locked by a foreign claim: an
  // operator has to be able to leave a modal they can no longer act in.
  const locked = busy !== null || Boolean(claimed);

  async function approve() {
    setBusy("approve");
    setError(null);
    try {
      const updated = await api.approveApproval(record.id);
      clearReasonDraft(record.id);
      onResolved(updated);
      notifyAgentChatReviewDecided(updated.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("approvals.approveFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function rejectWith(reasonText: string) {
    setBusy("reject");
    setError(null);
    try {
      const updated = await api.rejectApproval(record.id, reasonText ? { reason: reasonText } : {});
      clearReasonDraft(record.id);
      onResolved(updated);
      notifyAgentChatReviewDecided(updated.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("approvals.rejectFailed"));
    } finally {
      setBusy(null);
    }
  }

  function reject() {
    return rejectWith(reason);
  }

  // One click sends the preview's detected card error(s) back to the agent as
  // the rejection reason (surfaced in its tool result and the chat transcript),
  // no copy-paste needed. Bounded: a rejection reason is a message, not a log.
  function rejectWithConfigErrors() {
    const text = t("approvals.cardErrorReason", { errors: previewErrors.join("; ") });
    return rejectWith(text.length > 1200 ? `${text.slice(0, 1200)}...` : text);
  }

  const othersBanner = othersPending > 0 && onReviewAll ? (
    <div className="banner banner-info approval-others-banner">
      <span>{tn("approvals.othersPending", othersPending)}</span>
      <button type="button" className="btn btn-sm" onClick={onReviewAll}>{t("approvals.reviewAll")}</button>
    </div>
  ) : null;

  return (
    <Modal
      titleId="approval-detail-title"
      onClose={busy ? undefined : onClose}
      onNavigatePrevious={busy ? undefined : onNavigatePrevious}
      onNavigateNext={busy ? undefined : onNavigateNext}
      recordNavigation
      wide={view === "details"}
    >
      {view === "summary" ? (
        <div className="approval-summary-timeline">
          <section className="approval-summary-node approval-summary-proposal">
            <h3 className="modal-title approval-summary-title" id="approval-detail-title">{summary.title}</h3>
            <p className="approval-summary-body">{summary.body}</p>
          </section>

          {othersBanner}
          <div className="approval-summary-connector" aria-hidden="true" />

          {isPending ? (
            <section className="approval-summary-node approval-summary-command">
              {error && <ErrorMsg msg={error} />}
              {summaryRejectOpen && (
                <div className="approval-reject-row">
                  <label htmlFor="approval-summary-reason" className="approval-reason-label">
                    {t("approvalSummary.action.optionalReason")}
                  </label>
                  <input
                    ref={summaryReasonRef}
                    id="approval-summary-reason"
                    type="text"
                    value={reason}
                    onChange={(e) => { setReason(e.target.value); setReasonDraft(record.id, e.target.value); }}
                    className="approval-reason-input"
                    placeholder={t("approvalSummary.action.reasonPlaceholder")}
                    disabled={locked}
                  />
                </div>
              )}
              <div className="approval-summary-actions">
                <button className="btn btn-text" onClick={() => setView("details")}>
                  {t("approvalSummary.view.details")}
                </button>
                {summaryRejectOpen ? (
                  <>
                    <button className="btn" onClick={() => setSummaryRejectOpen(false)} disabled={locked}>
                      {t("approvalSummary.action.cancelReject")}
                    </button>
                    <button className="btn" onClick={reject} disabled={locked}>
                      {busy === "reject" ? t("approvalSummary.action.rejecting") : t("approvalSummary.action.reject")}
                    </button>
                  </>
                ) : (
                  <button className="btn" onClick={() => setSummaryRejectOpen(true)} disabled={locked}>
                    {t("approvalSummary.action.reject")}
                  </button>
                )}
                {!summaryRejectOpen && (
                  <button className="btn btn-primary approval-approve-btn" onClick={approve}
                    disabled={locked} aria-busy={busy === "approve" || claimed}
                    aria-label={busy === "approve" || claimed ? t("approvalSummary.action.approving") : undefined}>
                    {t("approvalSummary.action.approve")}
                  </button>
                )}
              </div>
            </section>
          ) : (
            <>
              <section className={`approval-summary-node approval-summary-status is-${record.status}`}>
                <div className="approval-summary-status-text">
                  <StatusBadge status={record.status} />
                  <span>{t("approvalSummary.history.resolvedAt", {
                    time: formatDateTime(record.resolved_at || record.created_at),
                  })}</span>
                </div>
                <button className="btn btn-text" onClick={() => setView("details")}>
                  {t("approvalSummary.view.details")}
                </button>
              </section>
              <div className="approval-summary-connector" aria-hidden="true" />
              <section className="approval-summary-node approval-summary-result">
                <p className="approval-summary-body">{approvalOutcome(record)}</p>
              </section>
              <div className="modal-actions approval-summary-close">
                <button className="btn btn-text" onClick={onClose}>{t("common.close")}</button>
              </div>
            </>
          )}
        </div>
      ) : (
        <>
          <div className="approval-detail-viewbar">
            <button className="btn btn-text" onClick={() => setView("summary")}>
              {t("approvalSummary.view.summary")}
            </button>
          </div>
          <h3 className="modal-title" id="approval-detail-title">
            {diffSummary(record.diff) || friendlyToolName(record.tool_name)}
          </h3>
          <div className="approval-detail-meta">
            <span className="stat-label">{t("approvals.metaToken")}</span><span>{record.token_name}</span>
            <span className="stat-label">{t("approvals.metaTool")}</span><span><code>{friendlyToolName(record.tool_name)}</code></span>
            <span className="stat-label">{t("approvals.metaCapability")}</span><span><code>{record.cap_name}</code></span>
            <span className="stat-label">{t("approvals.metaCreated")}</span><span>{formatDateTime(record.created_at)}</span>
            <span className="stat-label">{t("approvals.metaExpires")}</span><span>{formatDateTime(record.expires_at)}</span>
            <span className="stat-label">{t("approvals.metaStatus")}</span><span><StatusBadge status={record.status} /></span>
          </div>
          {othersBanner}
          {!isPending && record.rejected_reason && (
            <div className="banner banner-error">
              <strong>{t(record.status === "rejected"
                ? "approvals.resolvedRejected"
                : record.status === "cancelled" ? "approvals.resolvedCancelled" : "approvals.resolvedReason")}:</strong>{" "}
              {friendlyReason(record)}
            </div>
          )}
          <div className="approval-detail-tabs" role="tablist" aria-label={t("approvals.detailTablist")} onKeyDown={handleDetailTabKeyDown}>
            {detailTabs.map((tabName) => (
              <button
                key={tabName}
                id={`approval-detail-tab-${tabName}`}
                role="tab"
                aria-selected={activeTab === tabName}
                aria-controls="approval-detail-panel"
                tabIndex={activeTab === tabName ? 0 : -1}
                className={`approval-detail-tab${activeTab === tabName ? " active" : ""}`}
                onClick={() => switchDetailTab(tabName)}
              >
                {t(tabName === "diff" ? "approvals.detailTabDiff" : tabName === "args" ? "approvals.detailTabArgs" : "approvals.detailTabResult")}
              </button>
            ))}
          </div>
          <div
            id="approval-detail-panel"
            className={`approval-detail-body${isPending ? " approval-detail-body-pending" : ""}`}
            role="tabpanel"
            aria-labelledby={`approval-detail-tab-${activeTab}`}
          >
            {activeTab === "diff" && <DiffView record={record} onConfigErrors={onConfigErrors} />}
            {activeTab === "args" && <YamlView value={toYaml(record.args as Record<string, unknown>)} />}
            {activeTab === "result" && (record.result == null ? (
              <p className="approvals-empty">
                {record.rejected_reason
                  ? t("approvals.noResult", { status: approvalStatusLabel(record.status), reason: friendlyReason(record) })
                  : t("approvals.noResultRecorded", { status: approvalStatusLabel(record.status) })}
              </p>
            ) : <YamlView value={toYaml(record.result as Record<string, unknown>)} />)}
          </div>
          {error && <ErrorMsg msg={error} />}
          {isPending && (
            <>
              <div className="approval-reject-row">
                <label htmlFor="approval-reason" className="approval-reason-label">{t("approvals.reasonLabel")}</label>
                <input id="approval-reason" type="text" value={reason}
                  onChange={(e) => { setReason(e.target.value); setReasonDraft(record.id, e.target.value); }}
                  className="approval-reason-input" placeholder={t("approvals.reasonPlaceholder")} disabled={locked} />
              </div>
              <div className={`modal-actions approval-modal-actions${previewErrors.length === 0 ? " approval-modal-actions-compact" : ""}`}>
                <button className="btn btn-primary approval-approve-btn" onClick={approve}
                  disabled={locked} aria-busy={busy === "approve" || claimed}
                  aria-label={busy === "approve" || claimed ? t("approvals.approving") : t("approvals.approve")}>
                  <span className="btn-label-full approval-action-label">{t("approvals.approve")}</span>
                  <span className="btn-label-short approval-action-label">{t("agentchat.approve")}</span>
                </button>
                <button className="btn" onClick={reject} disabled={locked}
                  aria-busy={busy === "reject"}
                  aria-label={busy === "reject" ? t("approvals.rejecting") : t("approvals.reject")}>
                  <span className="approval-action-label">{t("approvals.reject")}</span>
                </button>
                {previewErrors.length > 0 && (
                  <button className="btn btn-outline" onClick={rejectWithConfigErrors} disabled={locked} title={t("approvals.rejectWithErrorTitle")}>
                    {t("approvals.rejectWithError")}
                  </button>
                )}
                <button className="btn btn-text" onClick={onClose} disabled={busy !== null}>
                  <span className="approval-action-label">{t("common.close")}</span>
                </button>
              </div>
            </>
          )}
          {!isPending && <div className="modal-actions"><button className="btn btn-text" onClick={onClose}>{t("common.close")}</button></div>}
        </>
      )}
    </Modal>
  );
}

const CARD_OP_TOOLS = new Set(["add_dashboard_card", "edit_dashboard_card", "delete_dashboard_card"]);

/** True when a patch_dashboard record addresses a CARD, so the card preview applies.
 *
 * patch_dashboard can target anything in a layout, and most of it is not a card:
 * a view title is a string, and a badge is its own config shape that would render
 * as something else entirely if fed to hui-card. Previewing those would show the
 * approver a picture of something that is not what they are approving, which is
 * worse than showing no picture, so the preview is offered only when the path
 * runs through a "cards" list. Everything else falls back to the text diff.
 */
function isCardPatchRecord(record: { tool_name: string; args?: Record<string, unknown> | null }): boolean {
  if (record.tool_name !== "patch_dashboard") return false;
  const path = record.args?.path;
  return Array.isArray(path) && path.includes("cards");
}

/** A patch whose op removes the value has only a Before side to show. */
function isRemovePatch(record: { tool_name: string; args?: Record<string, unknown> | null }): boolean {
  return record.tool_name === "patch_dashboard" && record.args?.op === "remove";
}

function DiffView({ record, onConfigErrors }: {
  record: ApprovalRecord;
  onConfigErrors?: (messages: string[]) => void;
}) {
  const diff = record.diff || {};
  const kind = diff.kind || "system_action";
  const huiReady = useHuiCardReady();
  // Diff vs Preview is a remembered preference (storedPreviewMode), so an
  // operator who chose Preview once gets it on every later record too.
  const [mode, setModeState] = useState<"diff" | "preview">(() => storedPreviewMode());
  const setMode = (m: "diff" | "preview") => {
    rememberPreviewMode(m);
    setModeState(m);
  };
  // A card op previews Before (the card being removed/replaced) or After (the
  // card being added/that replaced it); a whole-layout write only ever has an
  // After to show (see below). Defaults to whichever side a delete has.
  const showsBeforeFirst = record.tool_name === "delete_dashboard_card" || isRemovePatch(record);
  const [side, setSide] = useState<"before" | "after">(showsBeforeFirst ? "before" : "after");
  useEffect(() => {
    setModeState(storedPreviewMode());
    setSide(showsBeforeFirst ? "before" : "after");
  }, [record.id, record.tool_name, showsBeforeFirst]);

  const isWholeLayout = record.tool_name === "set_dashboard_config";
  // Energy previews render the configuration itself rather than a Lovelace
  // layout, so they bypass the hui-card readiness gate entirely.
  const energyPreview = (diff.preview as Record<string, unknown> | undefined)?.energy as
    { before?: unknown; after?: unknown } | undefined;
  const isCardOp = CARD_OP_TOOLS.has(record.tool_name) || isCardPatchRecord(record);
  // Whole-layout writes: only args.config (the full proposed layout) is
  // previewable. diff.before/after are truncated strings and the record
  // carries no untruncated current config, so only the After side exists.
  const wholeConfig = isWholeLayout ? (record.args?.config as unknown) : null;
  // Card ops: the After side comes from args.card, the full untruncated
  // object the approval carries for the executor (a single big card, e.g. a
  // multi-series chart, can exceed the diff string's truncation bound, and a
  // truncated diff.after does not parse; live-found). The Before side (the
  // prior card on edit/delete) only exists in diff.before, wrapped from its
  // JSON; unparseable disables just that side.
  const beforeCardConfig = isCardOp ? singleCardPreviewConfig(diff.before ?? null) : null;
  // patch_dashboard carries its card under args.value rather than args.card; both
  // are the full untruncated object the executor will apply.
  const afterCardConfig = isCardOp
    ? (wrapCardPreviewConfig(record.args?.card ?? record.args?.value)
       ?? singleCardPreviewConfig(diff.after ?? null))
    : null;
  const cardSideOk = {
    before: beforeCardConfig !== null && collectPreviewViews(beforeCardConfig) !== null,
    after: afterCardConfig !== null && collectPreviewViews(afterCardConfig) !== null,
  };
  const canPreview = !!energyPreview || (huiReady && (
    isWholeLayout ? collectPreviewViews(wholeConfig) !== null
    : isCardOp ? (cardSideOk.before || cardSideOk.after)
    : false
  ));
  // If the selected side is not actually previewable for this op (e.g. "after"
  // on a delete, which has none), fall back to whichever side is.
  const previewSide = cardSideOk[side] ? side : (cardSideOk.before ? "before" : "after");
  const previewConfig = isWholeLayout ? wholeConfig : (previewSide === "before" ? beforeCardConfig : afterCardConfig);

  if (kind === "yaml_diff" || kind === "config_diff" || kind === "file_write"
      || kind === "esphome_yaml") {
    const modeToggle = canPreview ? (
      <SegmentedToggle
        value={mode}
        onChange={setMode}
        ariaLabel={t("approvals.diffModeAria")}
        options={[{ value: "diff", label: t("common.diff") }, { value: "preview", label: t("common.preview") }]}
      />
    ) : undefined;
    if (!canPreview || mode !== "preview") {
      // Diff mode: the Diff|Preview switch rides in the diff's own toolbar,
      // right next to the side-by-side/stacked layout toggle.
      return <BeforeAfter before={diff.before ?? null} after={diff.after ?? null} toolbarExtra={modeToggle} />;
    }
    if (energyPreview) {
      return (
        <div className="approval-preview-pane">
          {modeToggle && <div className="approval-preview-toolbar">{modeToggle}</div>}
          <EnergyPreview energy={energyPreview as { before?: EnergyRows; after?: EnergyRows }} />
        </div>
      );
    }
    const showSideToggle = isCardOp && cardSideOk.before && cardSideOk.after;
    return (
      <div className="change-diff-wrap">
        <div className="change-diff-toolbar">
          <span className="change-diff-hint">
            {isWholeLayout
              ? t("approvals.diffWholeLayout")
              : previewSide === "before" ? t("approvals.diffCardBefore") : t("approvals.diffCardAfter")}
          </span>
          {modeToggle}
        </div>
        {showSideToggle && (
          <div className="diff-mode-row">
            <SegmentedToggle
              value={previewSide}
              onChange={setSide}
              ariaLabel={t("approvals.diffSideAria")}
              options={[{ value: "before", label: t("common.before") }, { value: "after", label: t("common.after") }]}
            />
          </div>
        )}
        <DashboardPreview key={previewSide} config={previewConfig} onConfigErrors={onConfigErrors} />
      </div>
    );
  }
  if (kind === "service_preview") {
    return <ServicePreview preview={diff.preview || {}} />;
  }
  return <SystemActionPreview summary={diffSummary(diff)} preview={diff.preview || {}} before={diff.before ?? null} />;
}

// Render a preview value for the review UI. Nested objects (e.g. service_data)
// are flattened to "key: value" pairs so they never show as "[object Object]".
function renderPreviewValue(v: unknown): string {
  if (v == null) return t("common.none");
  if (Array.isArray(v)) return v.length ? v.join(t("common.listSeparator")) : t("common.none");
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>);
    if (entries.length === 0) return t("common.none");
    return entries
      .map(([k, val]) => `${k}: ${val !== null && typeof val === "object" ? JSON.stringify(val) : String(val)}`)
      .join(t("common.listSeparator"));
  }
  return String(v);
}

function ServicePreview({ preview }: { preview: Record<string, unknown> }) {
  const mesa = preview.mesa as Record<string, unknown> | undefined;
  return (
    <div>
      <div className="approval-detail-meta">
        {Object.entries(preview).filter(([k]) => k !== "mesa").map(([k, v]) => (
          <React.Fragment key={k}>
            <span className="stat-label">{k}</span>
            <span><code>{renderPreviewValue(v)}</code></span>
          </React.Fragment>
        ))}
      </div>
      {mesa && <MesaPreviewBlock mesa={mesa} />}
    </div>
  );
}

function MesaPreviewBlock({ mesa }: { mesa: Record<string, unknown> }) {
  const confirm = (mesa.confirm_entities as string[]) ?? [];
  const allowed = (mesa.allowed_entities as string[]) ?? [];
  const blocked = (mesa.blocked as Array<{ entity_id: string; rule: string }>) ?? [];
  const warnings = (mesa.warnings as string[]) ?? [];
  return (
    <div className="mesa-preview-block">
      <div className="approval-detail-meta">
        <span className="stat-label">{t("approvals.mesaConfirm")}</span>
        <span><code>{confirm.length ? confirm.join(t("common.listSeparator")) : t("common.none")}</code></span>
        <span className="stat-label">{t("approvals.mesaAllowed")}</span>
        <span><code>{allowed.length ? allowed.join(t("common.listSeparator")) : t("common.none")}</code></span>
        {blocked.length > 0 && (
          <>
            <span className="stat-label">{t("approvals.mesaBlocked")}</span>
            <span><code>{blocked.map((b) => `${b.entity_id} (${b.rule})`).join(t("common.listSeparator"))}</code></span>
          </>
        )}
      </div>
      {warnings.length > 0 && (
        <ul className="mesa-preview-warnings">
          {warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>
  );
}

// The visual Preview for an Energy edit. Energy has no Lovelace layout, so the
// hui-card machinery the dashboard preview uses has nothing to render, and HA's
// own energy cards read preferences from the backend collection rather than from
// card config, so one embedded here would show the CURRENT dashboard regardless
// of what the approval proposes. Showing the operator something that is not what
// they are approving is worse than showing no picture, the same rule that limits
// patch_dashboard's preview to card paths. So this renders the configuration
// itself, with what changed marked.
type EnergySourceRow = { type?: string; name?: string; meters?: Array<[string, string]> };
type EnergyDeviceRow = { name?: string; statistic?: string };
type EnergyRows = { sources?: EnergySourceRow[]; devices?: EnergyDeviceRow[] };

// A raw slug interpolated into the UI stays English in every locale, so the type
// resolves through the catalog like every other server-side enum in this panel.
const ENERGY_SOURCE_LABEL_KEYS: Record<string, string> = {
  grid: "approvals.energyGrid",
  solar: "approvals.energySolar",
  battery: "approvals.energyBattery",
  gas: "approvals.energyGas",
  water: "approvals.energyWater",
};

type EnergyRow = { key: string; state: "added" | "removed" | "changed" | "same"; title: string; line: string };

/** Pair before/after rows into one ordered, diffed list.
 *
 * Keyed by STATISTIC for devices and by TYPE for sources, never by display name:
 * a rename would otherwise read as an unrelated removal plus addition instead of
 * the one substitution it is. Removed keys keep their original position so the
 * old and new values render adjacent rather than pages apart.
 */
function diffEnergyRows<T>(
  before: T[], after: T[], key: (r: T) => string, title: (r: T) => string, line: (r: T) => string,
): EnergyRow[] {
  const keys: string[] = [];
  for (const r of [...before, ...after]) if (!keys.includes(key(r))) keys.push(key(r));
  const out: EnergyRow[] = [];
  for (const k of keys) {
    const b = before.find((r) => key(r) === k);
    const a = after.find((r) => key(r) === k);
    if (b && !a) out.push({ key: k, state: "removed", title: title(b), line: line(b) });
    else if (a && !b) out.push({ key: k, state: "added", title: title(a), line: line(a) });
    else if (a && b && (title(a) !== title(b) || line(a) !== line(b))) {
      // Shown twice, old then new, so the operator reads the substitution rather
      // than having to infer it from one highlighted row.
      out.push({ key: `${k}-b`, state: "changed", title: title(b), line: line(b) });
      out.push({ key: `${k}-a`, state: "added", title: title(a), line: line(a) });
    } else if (a) out.push({ key: k, state: "same", title: title(a), line: line(a) });
  }
  return out;
}

function EnergyPreviewSection({ label, rows }: { label: string; rows: EnergyRow[] }) {
  return (
    <div className="energy-preview-section">
      <div className="energy-preview-heading">{label}</div>
      {rows.length === 0 && <div className="energy-preview-empty">{t("common.none")}</div>}
      {rows.map((r) => (
        <div key={r.key} className={`energy-preview-row is-${r.state}`}>
          <span className="energy-preview-name">{r.title}</span>
          <code className="energy-preview-stat">{r.line}</code>
        </div>
      ))}
    </div>
  );
}

export function EnergyPreview({ energy }: { energy: { before?: EnergyRows; after?: EnergyRows } }) {
  const before = energy.before ?? {};
  const after = energy.after ?? {};
  const sourceTitle = (r: EnergySourceRow) =>
    r.name || (r.type ? t(ENERGY_SOURCE_LABEL_KEYS[r.type] ?? "") || r.type : "");
  const sourceLine = (r: EnergySourceRow) =>
    (r.meters ?? []).map(([, stat]) => stat).join(t("common.listSeparator"));
  return (
    <div className="energy-preview">
      <EnergyPreviewSection
        label={t("approvals.energySources")}
        rows={diffEnergyRows(before.sources ?? [], after.sources ?? [],
          (r) => r.type ?? "", sourceTitle, sourceLine)}
      />
      <EnergyPreviewSection
        label={t("approvals.energyDevices")}
        rows={diffEnergyRows(before.devices ?? [], after.devices ?? [],
          (r) => r.statistic ?? "", (r) => r.name || r.statistic || "", (r) => r.statistic ?? "")}
      />
    </div>
  );
}

function SystemActionPreview({ summary, preview, before }: { summary?: string; preview: Record<string, unknown>; before?: string | null }) {
  return (
    <div>
      {summary && <p className="approval-summary-line"><strong>{summary}</strong></p>}
      {before && (
        <div className="yaml-diff-col">
          <div className="approval-diff-label">{t("approvals.diffRemoving")}</div>
          <RemovedPane value={before} />
        </div>
      )}
      {Object.keys(preview).length > 0 && (
        <YamlView value={toYaml(preview)} />
      )}
    </div>
  );
}
