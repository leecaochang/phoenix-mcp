import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Modal } from "./Modal";
import { DocsHelpLink } from "./common";
import { formatDateTime } from "../utils";
import type { AgentCliInstance, AgentCliProviderKind } from "../types";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";

// Alphabetical by label so the provider dropdown reads in order. Exported for
// the onboarding wizard's provider-setup step, which mirrors this add flow.
export const KINDS: { kind: AgentCliProviderKind; label: string; labelKey?: string; keyless: boolean }[] = [
  // Labels name the VENDOR, not the model family, so an account reads as the
  // credential it holds ("Anthropic", not "Claude"). The `kind` keys are the
  // stored identifiers and stay as they are. Ordered alphabetically by label.
  { kind: "claude", label: "Anthropic", keyless: false },
  { kind: "deepseek", label: "DeepSeek", keyless: false },
  { kind: "gemini", label: "Gemini", keyless: false },
  { kind: "grok", label: "Grok", keyless: false },
  { kind: "kimi", label: "Kimi", keyless: false },
  { kind: "meta", label: "Meta", keyless: false },
  { kind: "minimax", label: "MiniMax", keyless: false },
  { kind: "nvidia", label: "NVIDIA", keyless: false },
  { kind: "ollama_cloud", label: "Ollama (cloud)", labelKey: "settings.providerOllamaCloud", keyless: false },
  { kind: "ollama", label: "Ollama (local)", labelKey: "settings.providerOllamaLocal", keyless: true },
  { kind: "chatgpt", label: "OpenAI", keyless: false },
  { kind: "openrouter", label: "OpenRouter", keyless: false },
];

// Only the two Ollama entries carry a labelKey: the rest are bare brand names
// with nothing to translate, and inventing a catalog entry per provider would
// just be a list of identical strings in every locale.
export function kindLabel(k: { label: string; labelKey?: string }): string {
  return k.labelKey ? t(k.labelKey) : k.label;
}

// Which account warnings the operator has closed. Persisted, because the point
// of closing one is that it stays closed across visits; an in-memory dismissal
// would come straight back on the next card open, which is the complaint.
//
// The key includes the MODEL, so a warning about a different model is a
// different warning and shows again on its own. That is what keeps a dismissal
// from silently covering a NEW problem: the operator dismissed a statement about
// one model, not a category of statement forever.
const DISMISS_KEY = "phx-agentcli-dismissed";

function readDismissed(): Record<string, true> {
  try {
    const raw = window.localStorage.getItem(DISMISS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, true>) : {};
  } catch {
    return {};
  }
}

function writeDismissed(next: Record<string, true>) {
  try {
    window.localStorage.setItem(DISMISS_KEY, JSON.stringify(next));
  } catch { /* private mode: the warning simply keeps showing, which is safe */ }
}

/** A warning the operator can close, leaving a marker that reopens it. */
function DismissibleWarning({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="banner banner-warn agentcli-warn">
      <span>{text}</span>
      <button type="button" className="agentcli-warn-close" onClick={onDismiss}
              aria-label={t("settings.agentcliDismissWarning")}>&times;</button>
    </div>
  );
}

/** The marker a closed warning leaves behind, next to the model it is about. */
function WarningBadge({ text, onShow }: { text: string; onShow: () => void }) {
  return (
    <button type="button" className="agentcli-warn-badge" onClick={onShow}
            title={text} aria-label={text}>!</button>
  );
}

function notifyChanged() {
  window.dispatchEvent(new CustomEvent("phx-agentcli-providers-changed"));
}

interface Props {
  scrollback: number;
  onScrollbackChange: (n: number) => void;
  maxIterations: number;
  onMaxIterationsChange: (n: number) => void;
  globalVisible: boolean;
  onGlobalChange: (v: boolean) => void;
  saving: boolean;
}

interface FormState {
  credential: string;   // api key, or base_url for Ollama
  model: string;
  models: string[];
  validating: boolean;
  validated: boolean;
  error: string | null;
}

const EMPTY_FORM: FormState = { credential: "", model: "", models: [], validating: false, validated: false, error: null };

export function AgentCliSettings({ scrollback, onScrollbackChange, maxIterations, onMaxIterationsChange, globalVisible, onGlobalChange, saving }: Props) {
  const [instances, setInstances] = useState<AgentCliInstance[] | null>(null);
  const [newKind, setNewKind] = useState<AgentCliProviderKind>("claude");
  const [adding, setAdding] = useState<AgentCliProviderKind | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState<AgentCliInstance | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [scrollInput, setScrollInput] = useState(String(scrollback));
  const [maxIterInput, setMaxIterInput] = useState(String(maxIterations));

  // Each account's live model list, keyed by instance id. Absent means "not
  // checked or the provider could not be reached", which is NOT the same as an
  // empty list and must never be reported as one: a provider that is down would
  // otherwise be indistinguishable from one that dropped every model.
  const [liveModels, setLiveModels] = useState<Record<string, string[]>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [modelError, setModelError] = useState<string | null>(null);

  const load = () => api.getAgentCliProviders().then((r) => setInstances(r.instances)).catch(() => setInstances([]));
  useEffect(() => { void load(); }, []);

  // Refresh every account's model list when the card is shown. This is the free
  // half of staleness detection: a model list is a cheap authenticated GET, so
  // it costs no completion tokens, and it runs exactly when the operator is
  // looking at providers and can act on what it says. Best-effort and parallel;
  // a provider that fails simply records nothing.
  useEffect(() => {
    if (!instances?.length) return;
    let cancelled = false;
    void Promise.all(instances.map(async (inst) => {
      try {
        const r = await api.getAgentCliModels(inst.id);
        return [inst.id, r.models] as const;
      } catch {
        return null;
      }
    })).then((pairs) => {
      if (cancelled) return;
      setLiveModels(Object.fromEntries(pairs.filter((p): p is readonly [string, string[]] => p !== null)));
    });
    return () => { cancelled = true; };
  }, [instances]);

  /** The account's default model is set to something this provider no longer offers.
   *
   *  Only claimed against a non-empty list we actually fetched. An unreachable
   *  provider, or one that reports nothing, says nothing about the model, and
   *  reporting it as retired would send the operator to fix a working account.
   */
  const modelIsStale = (inst: AgentCliInstance) => {
    const models = liveModels[inst.id];
    return Boolean(inst.model && models && models.length > 0 && !models.includes(inst.model));
  };

  // Explicit refresh: re-read the model list AND whatever the provider declares
  // about each model. Separate from the on-open list fetch because the two cost
  // very different things: a model list is one request, while Ollama's
  // capabilities are one request PER MODEL. Still no completion tokens.
  const [dismissed, setDismissed] = useState<Record<string, true>>(readDismissed);

  // A warning is identified by the account, which warning it is, and the model
  // it is about, so changing the model retires the dismissal with it.
  const warnKey = (inst: AgentCliInstance, kind: string) => `${inst.id}|${kind}|${inst.model}`;
  const isDismissed = (inst: AgentCliInstance, kind: string) => dismissed[warnKey(inst, kind)] === true;
  const setDismissedFlag = (inst: AgentCliInstance, kind: string, on: boolean) =>
    setDismissed((prev) => {
      const next = { ...prev };
      if (on) next[warnKey(inst, kind)] = true;
      else delete next[warnKey(inst, kind)];
      writeDismissed(next);
      return next;
    });

  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [refreshResult, setRefreshResult] = useState<Record<string, string>>({});

  const refresh = async (inst: AgentCliInstance) => {
    setRefreshing(inst.id);
    setRefreshResult((r) => ({ ...r, [inst.id]: "" }));
    try {
      const r = await api.refreshAgentCliProvider(inst.id);
      setLiveModels((m) => ({ ...m, [inst.id]: r.models }));
      await load();
      setRefreshResult((res) => ({
        ...res,
        // "This provider publishes none" is a real answer and must not read like
        // a failed refresh: most providers report an id and an owner and nothing
        // else, so an empty result is the norm rather than a fault.
        [inst.id]: r.declared
          ? t("settings.agentcliRefreshed", { models: r.models.length })
          : t("settings.agentcliRefreshedNoCaps", { models: r.models.length }),
      }));
    } catch (err: unknown) {
      setRefreshResult((res) => ({
        ...res,
        [inst.id]: err instanceof Error ? err.message : t("settings.agentcliConnectionFailed"),
      }));
    } finally {
      setRefreshing(null);
    }
  };

  /** This model declared it cannot call tools, so Agent Chat cannot use it.
   *
   *  Surfaced ON THE OPTION rather than as a banner, which is where the two
   *  live reports landed. Listing every unusable model was noise: OpenRouter
   *  carries hundreds and already filters its own dropdown to the tool-capable
   *  ones, so the banner named sixty-odd models the operator could not select
   *  even if they wanted to. And it could not be dismissed, because there is
   *  nothing to fix: a local library simply contains models that cannot do
   *  this. A permanent banner about a choice nobody can make teaches the reader
   *  to skip banners, which is the one place a genuinely broken account has to
   *  be read. A BANNER now means only "this account is broken, fix it".
   */
  const cannotCallTools = (inst: AgentCliInstance, model: string) =>
    inst.capabilities?.[model]?.tools === false;

  const saveModel = async (id: string) => {
    setBusy(true);
    setModelError(null);
    try {
      await api.setAgentCliProviderModel(id, modelDraft);
      await load();
      notifyChanged();
      setEditing(null);
    } catch (err: unknown) {
      setModelError(err instanceof Error ? err.message : t("settings.agentcliSaveFailed"));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { setScrollInput(String(scrollback)); }, [scrollback]);
  useEffect(() => { setMaxIterInput(String(maxIterations)); }, [maxIterations]);

  const meta = (kind: AgentCliProviderKind) => KINDS.find((k) => k.kind === kind);

  const openAdd = () => { setAdding(newKind); setForm(EMPTY_FORM); };

  // Validate (probe) the entered credential and list models, WITHOUT storing.
  const validate = async () => {
    if (!adding) return;
    const value = form.credential.trim();
    if (!value) {
      setForm((f) => ({ ...f, error: meta(adding)?.keyless ? t("settings.agentcliEnterUrl") : t("settings.agentcliEnterKey") }));
      return;
    }
    setForm((f) => ({ ...f, validating: true, error: null }));
    try {
      const r = await api.probeAgentCliProvider(adding, meta(adding)?.keyless ? { base_url: value } : { api_key: value });
      if (!r.ok) {
        setForm((f) => ({ ...f, validating: false, validated: false, models: [], error: r.error ?? t("settings.agentcliConnectionFailed") }));
        return;
      }
      setForm((f) => ({
        ...f, validating: false, validated: true, error: null,
        models: r.models, model: r.models.includes(f.model) ? f.model : (r.models[0] ?? ""),
      }));
    } catch (err: unknown) {
      setForm((f) => ({ ...f, validating: false, validated: false, models: [], error: err instanceof Error ? err.message : t("settings.agentcliConnectionFailed") }));
    }
  };

  // Commit a validated account (creates a new instance).
  const done = async () => {
    if (!adding) return;
    const value = form.credential.trim();
    setBusy(true);
    try {
      await api.createAgentCliProvider(adding, {
        ...(meta(adding)?.keyless ? { base_url: value } : { api_key: value }),
        ...(form.model ? { model: form.model } : {}),
      });
      await load();
      notifyChanged();
      setAdding(null);
    } catch (err: unknown) {
      setForm((f) => ({ ...f, error: err instanceof Error ? err.message : t("settings.agentcliSaveFailed") }));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setRemoveError(null);
    try {
      await api.deleteAgentCliProvider(id);
      await load();
      notifyChanged();
      setConfirmRemove(null);
    } catch (err: unknown) {
      // Keep the dialog open and tell the user; a silent failure looked like
      // the account was removed when it was not.
      setRemoveError(err instanceof Error ? err.message : t("settings.agentcliRemoveAccountFailed"));
    } finally {
      setBusy(false);
    }
  };

  // Commit the memory value. Takes the raw string explicitly so the debounced
  // path is not subject to a stale closure over scrollInput.
  const commitScrollback = (raw: string) => {
    let n = parseInt(raw, 10);
    if (Number.isNaN(n)) { setScrollInput(String(scrollback)); return; }
    n = Math.max(0, Math.min(5000, n));
    setScrollInput(String(n));
    if (n !== scrollback) onScrollbackChange(n);
  };

  // Auto-save shortly after the last change so the spinner arrows persist without
  // needing an explicit blur or Enter (the reported "spinner does not stick" bug).
  const commitTimer = useRef<number | undefined>(undefined);
  const onScrollInput = (raw: string) => {
    setScrollInput(raw);
    window.clearTimeout(commitTimer.current);
    commitTimer.current = window.setTimeout(() => commitScrollback(raw), 600);
  };
  const commitNow = (raw: string) => {
    window.clearTimeout(commitTimer.current);
    commitScrollback(raw);
  };
  useEffect(() => () => window.clearTimeout(commitTimer.current), []);

  // Max tool rounds before the "continue?" checkpoint (idea b). Same
  // debounced-auto-save pattern as the memory field; clamped to [3, 100].
  const maxIterTimer = useRef<number | undefined>(undefined);
  const commitMaxIter = (raw: string) => {
    let n = parseInt(raw, 10);
    if (Number.isNaN(n)) { setMaxIterInput(String(maxIterations)); return; }
    n = Math.max(3, Math.min(100, n));
    setMaxIterInput(String(n));
    if (n !== maxIterations) onMaxIterationsChange(n);
  };
  const onMaxIterInput = (raw: string) => {
    setMaxIterInput(raw);
    window.clearTimeout(maxIterTimer.current);
    maxIterTimer.current = window.setTimeout(() => commitMaxIter(raw), 600);
  };
  const commitMaxIterNow = (raw: string) => {
    window.clearTimeout(maxIterTimer.current);
    commitMaxIter(raw);
  };
  useEffect(() => () => window.clearTimeout(maxIterTimer.current), []);

  const addingMeta = adding ? meta(adding) : undefined;

  return (
    <div className="card">
      <h3 className="card-header">
        {t("settings.agentcliCard")}
        <DocsHelpLink path="agentcli.html" label={t("settings.agentcliCard")} />
      </h3>
      <p className="settings-info-note" style={{ marginTop: 0 }}>
        {t("settings.agentcliIntro")}
      </p>

      {/* Add a new account. */}
      <div className="agentcli-add-row">
        <select className="input input-auto" value={newKind} disabled={busy || adding !== null}
                onChange={(e) => setNewKind(e.target.value as AgentCliProviderKind)} aria-label={t("settings.agentcliProviderType")}>
          {KINDS.map((k) => <option key={k.kind} value={k.kind}>{kindLabel(k)}</option>)}
        </select>
        <button className="btn btn-outline btn-sm" disabled={busy || adding !== null} onClick={openAdd}>{t("settings.agentcliAddProvider")}</button>
      </div>

      {adding && (
        <div className="agentcli-settings-form agentcli-add-form">
          <div className="agentcli-settings-hint">{t("settings.agentcliAdding", { name: addingMeta ? kindLabel(addingMeta) : "" })}</div>
          {addingMeta?.keyless ? (
            <input placeholder="http://host:11434" aria-label={t("settings.agentcliServerUrl")} value={form.credential} disabled={form.validating || busy}
                   onChange={(e) => setForm((s) => ({ ...s, credential: e.target.value, validated: false }))} />
          ) : (
            <input type="password" placeholder={t("settings.agentcliApiKey")} aria-label={t("settings.agentcliApiKey")} value={form.credential} disabled={form.validating || busy}
                   onChange={(e) => setForm((s) => ({ ...s, credential: e.target.value, validated: false }))} />
          )}
          {form.validating && <div className="agentcli-settings-hint" role="status">{t("settings.agentcliValidating")}</div>}
          {form.error && <div className="banner banner-error" role="alert">{form.error}</div>}
          {form.validated && (
            <label className="agentcli-settings-model-row">
              <span>{t("settings.agentcliSelectModel")}</span>
              <select value={form.model} disabled={busy || !form.models.length}
                      onChange={(e) => setForm((s) => ({ ...s, model: e.target.value }))}>
                {form.models.length
                  ? form.models.map((m) => <option key={m} value={m}>{m}</option>)
                  : <option value="">{t("settings.agentcliNoModels")}</option>}
              </select>
            </label>
          )}
          <div className="agentcli-settings-form-actions">
            {form.validated ? (
              <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void done()}>{t("settings.agentcliDone")}</button>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={busy || form.validating} onClick={() => void validate()}>{t("settings.agentcliValidate")}</button>
            )}
            <button className="btn btn-sm" disabled={busy} onClick={() => setAdding(null)}>{t("settings.cancel")}</button>
          </div>
        </div>
      )}

      {/* Configured accounts. */}
      <div className="agentcli-settings-providers">
        {(instances ?? []).map((inst) => (
          <div key={inst.id} className="agentcli-settings-provider">
            <div className="agentcli-settings-provider-head">
              <span>{inst.name}</span>
            </div>
            {modelIsStale(inst) && !isDismissed(inst, "retired") && (
              <DismissibleWarning
                text={t("settings.agentcliModelRetired", { model: inst.model })}
                onDismiss={() => setDismissedFlag(inst, "retired", true)}
              />
            )}
            {editing === inst.id ? (
              <div className="agentcli-settings-model-edit">
                <select value={modelDraft} disabled={busy}
                        aria-label={t("settings.agentcliSelectModel")}
                        onChange={(e) => setModelDraft(e.target.value)}>
                  {(liveModels[inst.id] ?? []).map((m) => (
                    <option key={m} value={m} disabled={cannotCallTools(inst, m)}>
                      {cannotCallTools(inst, m) ? t("settings.agentcliModelNoTools", { model: m }) : m}
                    </option>
                  ))}
                  {/* The stored model when the provider no longer lists it. It has
                      to be PRESENT or the select silently displays some other
                      model as though it were the configured one, and DISABLED or
                      the card offers a choice it has just finished warning about.
                      Showing it without disabling it was the first attempt and it
                      let a deleted model be re-selected. */}
                  {modelIsStale(inst) && (
                    <option value={inst.model} disabled>
                      {t("settings.agentcliModelUnavailable", { model: inst.model })}
                    </option>
                  )}
                </select>
                <button className="btn btn-primary btn-sm"
                        disabled={busy || !modelDraft || modelDraft === inst.model}
                        onClick={() => void saveModel(inst.id)}>{t("common.save")}</button>
                <button className="btn btn-text btn-sm" disabled={busy}
                        onClick={() => { setEditing(null); setModelError(null); }}>{t("settings.cancel")}</button>
              </div>
            ) : (
              <div className="agentcli-settings-provider-actions">
                <span className="agentcli-settings-model">
                  {t("settings.agentcliDefaultModel", { model: inst.model || t("settings.agentcliNotSet") })}
                  {modelIsStale(inst) && isDismissed(inst, "retired") && (
                    <WarningBadge text={t("settings.agentcliModelRetired", { model: inst.model })}
                                  onShow={() => setDismissedFlag(inst, "retired", false)} />
                  )}
                  {inst.model && cannotCallTools(inst, inst.model) && isDismissed(inst, "notools") && (
                    <WarningBadge text={t("settings.agentcliDefaultNoTools", { model: inst.model })}
                                  onShow={() => setDismissedFlag(inst, "notools", false)} />
                  )}
                </span>
                <button className="btn btn-sm" disabled={busy}
                        onClick={() => { setEditing(inst.id); setModelDraft(inst.model); setModelError(null); }}>
                  {t("settings.agentcliChangeModel")}
                </button>
                <button className="btn btn-sm" disabled={busy || refreshing !== null}
                        onClick={() => void refresh(inst)}>
                  {refreshing === inst.id ? t("settings.agentcliRefreshing") : t("settings.agentcliRefresh")}
                </button>
                <button className="btn btn-sm" disabled={busy} onClick={() => setConfirmRemove(inst)}>{t("settings.remove")}</button>
              </div>
            )}
            {refreshResult[inst.id] && (
              <div className="agentcli-settings-hint" role="status">{refreshResult[inst.id]}</div>
            )}
            {inst.capabilities_checked_at && (
              <div className="agentcli-settings-hint">
                {t("settings.agentcliCapsChecked", { when: formatDateTime(inst.capabilities_checked_at) })}
              </div>
            )}
            {inst.model && cannotCallTools(inst, inst.model) && !isDismissed(inst, "notools") && (
              <DismissibleWarning
                text={t("settings.agentcliDefaultNoTools", { model: inst.model })}
                onDismiss={() => setDismissedFlag(inst, "notools", true)}
              />
            )}
            {editing === inst.id && modelError && (
              <div className="banner banner-error" role="alert">{modelError}</div>
            )}
          </div>
        ))}
        {instances && instances.length === 0 && !adding && (
          <div className="agentcli-settings-hint">{t("settings.agentcliNoAccounts")}</div>
        )}
      </div>

      {confirmRemove && (
        <Modal titleId="agentcli-remove-title" onClose={() => { setConfirmRemove(null); setRemoveError(null); }}>
          <h3 className="modal-title" id="agentcli-remove-title">{t("settings.agentcliRemoveTitle")}</h3>
          <p>{tRich("settings.agentcliRemoveBody", { strong: (c) => <strong>{c}</strong> }, { name: confirmRemove.name })}</p>
          {removeError && <div className="banner banner-error" role="alert">{removeError}</div>}
          <div className="modal-actions">
            <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void remove(confirmRemove.id)}>{t("settings.remove")}</button>
            <button className="btn btn-text" disabled={busy} onClick={() => { setConfirmRemove(null); setRemoveError(null); }}>{t("settings.cancel")}</button>
          </div>
        </Modal>
      )}

      <hr className="settings-divider" />
      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span>{t("settings.agentcliGlobalLabel")}</span>
          <small>
            {t("settings.agentcliGlobalHelp")}
          </small>
        </div>
        <label className={`toggle-switch${saving ? " disabled" : ""}`}>
          <input
            type="checkbox"
            aria-label={t("settings.agentcliGlobalAria")}
            checked={globalVisible}
            disabled={saving}
            onChange={(e) => onGlobalChange(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
      <div className="toggle-row toggle-row-plain" style={{ marginTop: 14 }}>
        <div className="toggle-label">
          <span>{t("settings.agentcliScrollbackLabel")}</span>
          <small>
            {t("settings.agentcliScrollbackHelp")}
          </small>
        </div>
        <input
          className="input input-auto"
          type="number" min={0} max={5000} step={100}
          aria-label={t("settings.agentcliScrollbackAria")}
          value={scrollInput}
          disabled={saving}
          onChange={(e) => onScrollInput(e.target.value)}
          onBlur={() => commitNow(scrollInput)}
          onKeyDown={(e) => { if (e.key === "Enter") commitNow(scrollInput); }}
        />
      </div>
      <div className="toggle-row toggle-row-plain" style={{ marginTop: 14 }}>
        <div className="toggle-label">
          <span>{t("settings.agentcliStepsLabel")}</span>
          <small>
            {t("settings.agentcliStepsHelp")}
          </small>
        </div>
        <input
          className="input input-auto"
          type="number" min={3} max={100} step={1}
          aria-label={t("settings.agentcliStepsAria")}
          value={maxIterInput}
          disabled={saving}
          onChange={(e) => onMaxIterInput(e.target.value)}
          onBlur={() => commitMaxIterNow(maxIterInput)}
          onKeyDown={(e) => { if (e.key === "Enter") commitMaxIterNow(maxIterInput); }}
        />
      </div>
    </div>
  );
}
