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
import { hasMessage, t } from "../i18n";

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
  /** Deep-link target from a notification (/phoenix-mcp#approvals/{id}); opens that approval. */
  openApprovalId?: string | null;
  /** Called once the deep-link has been consumed so the parent can clear it. */
  onConsumedDeepLink?: () => void;
}

const POLL_INTERVAL_MS = 10_000;
const HISTORY_PAGE = 50;
const HISTORY_FILTERS: (ApprovalStatus | "all")[] = ["all", "approved", "rejected", "expired", "cancelled"];
const FILTER_LABEL_KEYS: Record<string, string> = {
  all: "approvals.filterAll",
  approved: "approvals.filterApproved",
  rejected: "approvals.filterRejected",
  expired: "approvals.filterExpired",
  cancelled: "approvals.filterCancelled",
};

// Human-readable text for the backend's rejected_reason slugs. A free-text admin
// rejection reason is not in this map and is shown verbatim.
const REASON_LABEL_KEYS: Record<string, string> = {
  token_inactive: "approvals.reason.token_inactive",
  capability_denied: "approvals.reason.capability_denied",
  target_out_of_scope: "approvals.reason.target_out_of_scope",
  target_missing: "approvals.reason.target_missing",
  rate_limited_at_execution: "approvals.reason.rate_limited_at_execution",
  kill_switch: "approvals.reason.kill_switch",
  admin_cancelled: "approvals.reason.admin_cancelled",
  token_revoked: "approvals.reason.token_revoked",
  token_expired: "approvals.reason.token_expired",
  execution_failed: "approvals.reason.execution_failed",
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

/** Pull the tool's own error text out of a resolved approval's saved result. */
export function extractResultErrorText(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const toolResult = (result as Record<string, unknown>).tool_result;
  if (!toolResult || typeof toolResult !== "object") return null;
  const content = (toolResult as Record<string, unknown>).content;
  if (!Array.isArray(content)) return null;
  const texts = content
    .map((c) => (c && typeof c === "object" ? (c as Record<string, unknown>).text : null))
    .filter((t): t is string => typeof t === "string")
    .map((t) => t.trim())
    .filter(Boolean);
  return texts.length ? texts.join(" ") : null;
}

/** Readable rejection/cancellation reason: slug -> label, and for an
 *  execution_failed the underlying tool error (e.g. "Forbidden.", a validation
 *  message) instead of the bare slug. Free-text admin reasons pass through. */
export function friendlyReason(record: ApprovalRecord): string {
  const reason = record.rejected_reason;
  if (!reason) return "";
  if (reason === "execution_failed") {
    const detail = extractResultErrorText(record.result);
    if (detail) return t("approvals.reasonExecutionFailedDetail", { detail });
  }
  const key = REASON_LABEL_KEYS[reason];
  return key ? t(key) : reason;
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

  const loadPending = useCallback(async (offset = 0) => {
    setError(null);
    try {
      const resp = await api.listApprovals({ status: "pending", limit: HISTORY_PAGE, offset });
      setRecords((prev) => (offset === 0 ? resp.approvals : [...prev, ...resp.approvals]));
      setRawOffset(offset + resp.approvals.length);
      setHasMore(offset + resp.approvals.length < resp.total);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("approvals.loadFailed"));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  const loadHistory = useCallback(async (offset: number) => {
    setError(null);
    try {
      const resp = await api.listApprovals({
        status: histFilter === "all" ? undefined : histFilter,
        limit: HISTORY_PAGE,
        offset,
      });
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
      setError(e instanceof Error ? e.message : t("approvals.loadFailed"));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [histFilter]);

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
          setSelected(null);
          onCountChange?.();
        }
      })
      .catch(() => { if (!cancelled) setSelected(null); });  // gone/expired: close
    return () => { cancelled = true; };
  }, [refreshSignal, selected, onCountChange]);

  // Consume a notification deep-link: fetch the approval and open it.
  useEffect(() => {
    if (!openApprovalId) return;
    let cancelled = false;
    api.getApproval(openApprovalId)
      .then((rec) => {
        if (cancelled) return;
        onTabChange(rec.status === "pending" ? "pending" : "history");
        setSelected(rec);
      })
      .catch(() => { /* stale/unknown id: ignore */ })
      .finally(() => onConsumedDeepLink?.());
    return () => { cancelled = true; };
  }, [openApprovalId, onConsumedDeepLink, onTabChange]);

  function handleResolved(updated: ApprovalRecord) {
    setSelected(null);
    setRecords((prev) => prev.filter((r) => r.id !== updated.id));
    if (tab === "pending") loadPending();
    onCountChange?.();
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
      `${r.token_name} ${r.tool_name} ${friendlyToolName(r.tool_name)} ${diffSummary(r.diff)} ${r.rejected_reason ?? ""}`.toLowerCase().includes(q),
    );
  }, [records, search, tab]);

  // An approval already being executed cannot be batched: the server would answer
  // 409 on its claim and halt the run on something the operator never chose.
  const selectableIds = useMemo(
    () => (tab === "pending" ? shown.filter((r) => !isClaimed(r)).map((r) => r.id) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shown, tab, claimedApprovals],
  );
  const pickedCount = selectableIds.filter((id) => picked.has(id)).length;
  const allPicked = selectableIds.length > 0 && pickedCount === selectableIds.length;

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
          {selectableIds.length > 1 && (
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
                claimed={isClaimed(r)}
                checked={picked.has(r.id)}
                selectable={!isClaimed(r)}
                onToggle={() => togglePick(r.id)}
                onClick={() => setSelected(r)}
              />
            ))}
          </div>
        </>
      ) : (
        shown.length > 0 && (
          <div className="card approval-history">
            {shown.map((r) => (
              <HistoryRow key={r.id} record={r} onClick={() => setSelected(r)} />
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
          record={selected}
          claimed={isClaimed(selected)}
          onClose={() => setSelected(null)}
          onResolved={handleResolved}
        />
      )}
      </div>
    </div>
  );
}

function HistoryRow({ record, onClick }: { record: ApprovalRecord; onClick: () => void }) {
  const note = diffSummary(record.diff)
    || (record.rejected_reason
      ? t("approvals.reasonPrefix", { reason: friendlyReason(record) })
      : friendlyToolName(record.tool_name));
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
    rejected: "approvals.statusRejected",
    expired: "approvals.statusExpired",
    cancelled: "approvals.statusCancelled",
  };
  const cls: Record<ApprovalStatus, string> = {
    pending: "badge-amber",
    approved: "badge-green",
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

function ApprovalCard({ record, claimed, checked, selectable, onToggle, onClick }: {
  record: ApprovalRecord;
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
        {diffSummary(record.diff) || friendlyToolName(record.tool_name)}
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
  /** Its saved action is executing (claimed by an admin's Approve, possibly in
   *  another surface). Locks the actions exactly like this modal's own busy. */
  claimed?: boolean;
  onClose: () => void;
  onResolved: (updated: ApprovalRecord) => void;
}

function ApprovalDetailModal({ record, claimed, onClose, onResolved }: DetailProps) {
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
  useEffect(() => { setPreviewErrors([]); setReason(getReasonDraft(record.id)); }, [record.id]);
  const onConfigErrors = useCallback((messages: string[]) => {
    if (messages.length > 0) setPreviewErrors(messages);
  }, []);

  const isPending = record.status === "pending";
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

  return (
    <Modal titleId="approval-detail-title" onClose={busy ? undefined : onClose} wide>
      <h3 className="modal-title" id="approval-detail-title">
        {diffSummary(record.diff) || friendlyToolName(record.tool_name)}
      </h3>
      <div className="approval-detail-meta">
        <span className="stat-label">{t("approvals.metaToken")}</span>
        <span>{record.token_name}</span>
        <span className="stat-label">{t("approvals.metaTool")}</span>
        <span><code>{friendlyToolName(record.tool_name)}</code></span>
        <span className="stat-label">{t("approvals.metaCapability")}</span>
        <span><code>{record.cap_name}</code></span>
        <span className="stat-label">{t("approvals.metaCreated")}</span>
        <span>{formatDateTime(record.created_at)}</span>
        <span className="stat-label">{t("approvals.metaExpires")}</span>
        <span>{formatDateTime(record.expires_at)}</span>
        <span className="stat-label">{t("approvals.metaStatus")}</span>
        <span><StatusBadge status={record.status} /></span>
      </div>

      {!isPending && record.rejected_reason && (
        <div className="banner banner-error">
          <strong>
            {t(record.status === "rejected"
              ? "approvals.resolvedRejected"
              : record.status === "cancelled" ? "approvals.resolvedCancelled" : "approvals.resolvedReason")}:
          </strong>{" "}
          {friendlyReason(record)}
        </div>
      )}

      <div className="approval-detail-tabs" role="tablist" aria-label={t("approvals.detailTablist")} onKeyDown={handleDetailTabKeyDown}>
        <button
          id="approval-detail-tab-diff"
          role="tab"
          aria-selected={activeTab === "diff"}
          aria-controls="approval-detail-panel"
          tabIndex={activeTab === "diff" ? 0 : -1}
          className={`approval-detail-tab${activeTab === "diff" ? " active" : ""}`}
          onClick={() => switchDetailTab("diff")}
        >
          {t("approvals.detailTabDiff")}
        </button>
        <button
          id="approval-detail-tab-args"
          role="tab"
          aria-selected={activeTab === "args"}
          aria-controls="approval-detail-panel"
          tabIndex={activeTab === "args" ? 0 : -1}
          className={`approval-detail-tab${activeTab === "args" ? " active" : ""}`}
          onClick={() => switchDetailTab("args")}
        >
          {t("approvals.detailTabArgs")}
        </button>
        {!isPending && (
          <button
            id="approval-detail-tab-result"
            role="tab"
            aria-selected={activeTab === "result"}
            aria-controls="approval-detail-panel"
            tabIndex={activeTab === "result" ? 0 : -1}
            className={`approval-detail-tab${activeTab === "result" ? " active" : ""}`}
            onClick={() => switchDetailTab("result")}
          >
            {t("approvals.detailTabResult")}
          </button>
        )}
      </div>

      <div
        id="approval-detail-panel"
        className="approval-detail-body"
        role="tabpanel"
        aria-labelledby={`approval-detail-tab-${activeTab}`}
      >
        {activeTab === "diff" && <DiffView record={record} onConfigErrors={onConfigErrors} />}
        {activeTab === "args" && (
          <YamlView value={toYaml(record.args as Record<string, unknown>)} />
        )}
        {activeTab === "result" && (
          record.result == null ? (
            <p className="approvals-empty">
              {record.rejected_reason
                ? t("approvals.noResult", {
                    status: t(record.status === "rejected" ? "approvals.resolvedRejected" : "approvals.resolvedCancelled"),
                    reason: friendlyReason(record),
                  })
                : t("approvals.noResultRecorded", { status: approvalStatusLabel(record.status) })}
            </p>
          ) : (
            <YamlView value={toYaml(record.result as Record<string, unknown>)} />
          )
        )}
      </div>

      {error && <ErrorMsg msg={error} />}

      {isPending && (
        <>
          <div className="approval-reject-row">
            <label htmlFor="approval-reason" className="approval-reason-label">
              {t("approvals.reasonLabel")}
            </label>
            <input
              id="approval-reason"
              type="text"
              value={reason}
              onChange={(e) => { setReason(e.target.value); setReasonDraft(record.id, e.target.value); }}
              className="approval-reason-input"
              placeholder={t("approvals.reasonPlaceholder")}
              disabled={locked}
            />
          </div>
          <div className="modal-actions">
            <button
              className="btn btn-primary"
              onClick={approve}
              disabled={locked}
            >
              {busy === "approve" || claimed ? t("approvals.approving") : t("approvals.approve")}
            </button>
            {/* Reject is neutral, not danger. Reject-with-reason is the
                operator's iteration channel and several rounds is the normal
                authoring workflow, so styling it as destructive discourages the
                path the design intends; Approve being primary already carries
                the emphasis. Agent Chat's bubble matches. Genuinely destructive
                controls (revoke, wipe, delete) keep btn-danger. */}
            <button
              className="btn"
              onClick={reject}
              disabled={locked}
            >
              {busy === "reject" ? t("approvals.rejecting") : t("approvals.reject")}
            </button>
            {previewErrors.length > 0 && (
              <button
                className="btn btn-outline"
                onClick={rejectWithConfigErrors}
                disabled={locked}
                title={t("approvals.rejectWithErrorTitle")}
              >
                {t("approvals.rejectWithError")}
              </button>
            )}
            <button
              className="btn btn-text"
              onClick={onClose}
              disabled={busy !== null}
            >
              {t("common.close")}
            </button>
          </div>
        </>
      )}
      {!isPending && (
        <div className="modal-actions">
          <button className="btn btn-text" onClick={onClose}>{t("common.close")}</button>
        </div>
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
  const canPreview = huiReady && (
    isWholeLayout ? collectPreviewViews(wholeConfig) !== null
    : isCardOp ? (cardSideOk.before || cardSideOk.after)
    : false
  );
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
