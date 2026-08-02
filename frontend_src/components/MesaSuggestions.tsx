// The MESA tab's "Suggested profiles" section: rows computed by the backend
// scanner (blast-radius orchestrators, naked risky devices) with Apply /
// Review / Dismiss per row. Purely presentational; all api calls live in
// MesaView so this component stays trivially testable.
import { useState } from "react";
import { t, hasMessage } from "../i18n";
import type { MesaSuggestion } from "../types";

const SIGNAL_LABEL_KEYS: Record<string, string> = {
  blast_radius: "mesa.signalBlastRadius",
  naked_risky: "mesa.signalUnprotected",
};

// Same MESA control-mode vocabulary the profile editor and badges use.
/**
 * The suggestion sentence, in the operator's language.
 *
 * The backend stores English (it is written verbatim into control_reason when
 * the suggestion is applied, so it is the record). It also sends the template
 * key and its params, including keys for the sub-phrases the sentence splices
 * in: translating the sentence alone would leave an English clause inside it.
 * Falls back to the stored English whenever anything is missing, so an older
 * record or a new template still reads.
 */
function suggestionReason(s: MesaSuggestion): string {
  const key = s.reason_key ? `mesaSuggestion.${s.reason_key}` : "";
  if (!key || !hasMessage(key)) return s.reason;
  const params: Record<string, string | number> = { ...(s.reason_params ?? {}) };
  for (const [param, holder] of [["noun_key", "noun"], ["concern_key", "concern"], ["baseline_key", "baseline_note"]] as const) {
    const sub = params[param];
    if (typeof sub === "string" && sub && hasMessage(`mesaSuggestion.${sub}`)) {
      params[holder] = t(`mesaSuggestion.${sub}`);
    }
  }
  return t(key, params);
}

const MODE_LABEL_KEYS: Record<string, string> = {
  confirm: "mesa.modeConfirm",
  prohibited: "mesa.modeProhibited",
  read_only: "mesa.modeReadOnly",
  autonomous: "mesa.modeAutonomous",
};

const MODE_BADGE: Record<string, string> = {
  confirm: "badge-amber",
  prohibited: "badge-red",
  read_only: "badge-blue",
  autonomous: "badge-green",
};

export function MesaSuggestions({
  suggestions,
  dismissedCount,
  busyKey,
  rescanning,
  onApply,
  onReview,
  onDismiss,
  onRestoreAll,
  onRescan,
}: {
  suggestions: MesaSuggestion[];
  dismissedCount: number;
  busyKey: string | null;
  rescanning: boolean;
  onApply: (s: MesaSuggestion) => void;
  onReview: (s: MesaSuggestion) => void;
  onDismiss: (s: MesaSuggestion) => void;
  onRestoreAll: () => void;
  onRescan: () => void;
}) {
  // Collapsible, matching the domain/entity group cards below it on this tab
  // (same .collapsible-chevron). Expanded by default: this is where a new
  // finding needs to be noticed. Always rendered, even with zero findings,
  // so the Rescan affordance never disappears.
  const [open, setOpen] = useState(true);
  return (
    <div className="card mesa-suggestions">
      <div className="mesa-suggest-toolbar">
        <button
          type="button"
          className="mesa-suggest-toggle"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          <span className={`collapsible-chevron${open ? " open" : ""}`} aria-hidden="true" />
          <span>{t("mesa.suggestTitle")}</span>
          {suggestions.length > 0 && <span className="badge badge-amber mesa-suggest-count">{suggestions.length}</span>}
        </button>
        {open && (
          <button className="btn btn-outline btn-sm" onClick={onRescan} disabled={rescanning}>
            {rescanning ? t("mesa.scanning") : t("mesa.rescan")}
          </button>
        )}
      </div>
      {open && (
        <div className="mesa-suggest-body">
          <p className="mesa-suggest-sub">
            {t("mesa.suggestSub")}
          </p>
          {suggestions.length === 0 && (
            <p className="mesa-suggest-empty">{t("mesa.noOpenSuggestions")}</p>
          )}
          {suggestions.map((s) => {
            const busy = busyKey === s.key;
            return (
              <div key={s.key} className="preset-row mesa-suggest-row">
                <div className="mesa-suggest-main">
                  <div className="mesa-suggest-head">
                    <code>{s.scope === "domain" ? t("mesa.suggestDomainSubject", { id: s.subject_id }) : s.subject_id}</code>
                    <span className="mesa-suggest-badges">
                      <span className="badge badge-grey">{SIGNAL_LABEL_KEYS[s.signal] ? t(SIGNAL_LABEL_KEYS[s.signal]) : s.signal}</span>
                      <span className={`badge ${MODE_BADGE[s.suggested_mode] ?? "badge-grey"}`}>{MODE_LABEL_KEYS[s.suggested_mode] ? t(MODE_LABEL_KEYS[s.suggested_mode]) : s.suggested_mode}</span>
                    </span>
                  </div>
                  <div className="mesa-suggest-reason">{suggestionReason(s)}</div>
                </div>
                <span className="preset-row-actions">
                  <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => onApply(s)}>
                    {busy ? t("mesa.working") : t("mesa.apply")}
                  </button>
                  <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => onReview(s)}>
                    {t("mesa.review")}
                  </button>
                  <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onDismiss(s)} aria-label={t("mesa.dismissAria", { id: s.subject_id })}>
                    {t("mesa.dismiss")}
                  </button>
                </span>
              </div>
            );
          })}
          {dismissedCount > 0 && (
            <div className="mesa-suggest-footer">
              <span>{t("mesa.dismissedCount", { count: dismissedCount })}</span>
              <button className="btn btn-text btn-sm" onClick={onRestoreAll}>{t("mesa.restoreAll")}</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
