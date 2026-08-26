import { useEffect, useState } from "react";
import type { GlobalSettings, TokenRecord, AgentCliInstance, AiTaskPreferredStatus } from "../types";
import { api } from "../api";
import { Modal } from "./Modal";
import { DocsHelpLink } from "./common";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";

// AI Task settings card. Registers Phoenix MCP as a Home Assistant AI Task entity ("Phoenix MCP AI
// Task") so ai_task.generate_data can target Phoenix MCP and run its own model on the chosen
// token's scope, with MESA safety, approvals, and audit. Independent of the Voice
// Agent and the Assist bridge; needs a token, an Agent Chat provider account, a model.
export function AiTaskSettings({
  settings,
  onChange,
  saving,
}: {
  settings: GlobalSettings;
  onChange: (key: keyof GlobalSettings, value: boolean | string) => void;
  saving: boolean;
}) {
  const [tokens, setTokens] = useState<TokenRecord[]>([]);
  const [instances, setInstances] = useState<AgentCliInstance[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [pref, setPref] = useState<AiTaskPreferredStatus | null>(null);
  const [showSetDefault, setShowSetDefault] = useState(false);
  const [showDisableConfirm, setShowDisableConfirm] = useState(false);

  const supported = settings.ai_task_supported !== false;
  const enabled = !!settings.ai_task_enabled;
  const providerId = settings.ai_task_provider_id ?? "";
  const tokenId = settings.ai_task_token_id ?? "";
  const model = settings.ai_task_model ?? "";
  const noProviders = instances.length === 0;

  useEffect(() => {
    api.listTokens().then(setTokens).catch(() => setTokens([]));
    const loadProviders = () =>
      api.getAgentCliProviders().then((r) => setInstances(r.instances)).catch(() => setInstances([]));
    loadProviders();
    window.addEventListener("phx-agentcli-providers-changed", loadProviders);
    return () => window.removeEventListener("phx-agentcli-providers-changed", loadProviders);
  }, []);

  useEffect(() => {
    if (!providerId) { setModels([]); return; }
    let cancelled = false;
    api.getAgentCliModels(providerId)
      .then((r) => { if (!cancelled) setModels(r.models); })
      .catch(() => { if (!cancelled) setModels([]); });
    return () => { cancelled = true; };
  }, [providerId]);

  // The AI Task entity (and thus its eligibility to be the default) exists only when
  // fully configured, so re-pull the "default data-gen entity" status whenever the
  // config changes, not just on mount.
  const loadPref = () => {
    if (!supported) { setPref(null); return; }
    api.getAiTaskPreferred().then(setPref).catch(() => setPref(null));
  };
  useEffect(() => {
    loadPref();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported, enabled, tokenId, providerId, model]);

  // Throws on error so the confirm modal can surface it; updates status on success.
  async function setDefault() {
    setPref(await api.setAiTaskPreferred());
  }
  async function removeDefault() {
    try {
      setPref(await api.clearAiTaskPreferred());
    } catch {
      loadPref();
    }
  }

  function handleEnableToggle(checked: boolean) {
    // Disabling removes the Phoenix MCP AI Task entity, and clearing it as the default is a
    // visible HA change, so confirm first when Phoenix MCP is currently the default.
    if (!checked && pref?.is_preferred) {
      setShowDisableConfirm(true);
      return;
    }
    onChange("ai_task_enabled", checked);
    // On enable, immediately offer to make Phoenix MCP the default (mirrors the voice agent's
    // first-enable modal); the modal handles the not-yet-configured case.
    if (checked && !pref?.is_preferred && (pref?.supported ?? false)) {
      setShowSetDefault(true);
    }
  }

  return (
    <div className="card">
      <h3 className="card-header">
        {t("settings.aiTaskCard")}
        <DocsHelpLink path="connect.html#ai-task" label={t("settings.aiTaskCard")} />
      </h3>
      <p className="agentcli-settings-hint">
        {t("settings.aiTaskIntro")}
      </p>
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.aiTaskEnableLabel")}</span>
          <small>
            {supported
              ? t("settings.aiTaskEnableHelp")
              : t("settings.aiTaskUnsupported")}
          </small>
        </div>
        <label className={`toggle-switch${saving || !supported ? " disabled" : ""}`}>
          <input
            type="checkbox"
            aria-label={t("settings.aiTaskEnableLabel")}
            checked={enabled}
            disabled={saving || !supported}
            onChange={(e) => handleEnableToggle(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>

      <div className="toggle-row toggle-row-stacked-control">
        <div className="toggle-label">
          <span>{t("settings.token")}</span>
          <small>{t("settings.aiTaskTokenHelp")}</small>
        </div>
        <select
          aria-label={t("settings.aiTaskTokenAria")}
          className="input input-auto"
          value={tokenId}
          disabled={saving || !supported}
          onChange={(e) => onChange("ai_task_token_id", e.target.value)}
        >
          <option value="">{t("settings.selectToken")}</option>
          {tokens.map((tok) => (
            <option key={tok.id} value={tok.id}>{tok.name}</option>
          ))}
        </select>
      </div>

      <div className="toggle-row toggle-row-stacked-control">
        <div className="toggle-label">
          <span>{t("settings.providerAccount")}</span>
          <small>{noProviders ? t("settings.addProviderFirst") : t("settings.aiTaskProviderHelp")}</small>
        </div>
        <select
          aria-label={t("settings.aiTaskProviderAria")}
          className="input input-auto"
          value={providerId}
          disabled={saving || noProviders || !supported}
          onChange={(e) => onChange("ai_task_provider_id", e.target.value)}
        >
          <option value="">{t("settings.selectProvider")}</option>
          {instances.map((i) => (
            <option key={i.id} value={i.id}>{i.name}</option>
          ))}
        </select>
      </div>

      <div className="toggle-row toggle-row-stacked-control">
        <div className="toggle-label">
          <span>{t("settings.model")}</span>
          <small>{t("settings.aiTaskModelHelp")}</small>
        </div>
        <select
          aria-label={t("settings.aiTaskModelAria")}
          className="input input-auto"
          value={model}
          disabled={saving || !providerId || noProviders || !supported}
          onChange={(e) => onChange("ai_task_model", e.target.value)}
        >
          <option value="">{t("settings.selectModel")}</option>
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      {supported && (pref?.supported ?? false) && (
        <div className="toggle-row toggle-row-plain settings-toggle-mt">
          <div className="toggle-label">
            <span>{t("settings.aiTaskDefaultLabel")}</span>
            <small>
              {pref?.is_preferred
                ? t("settings.aiTaskDefaultOn")
                : t("settings.aiTaskDefaultOff")}
            </small>
          </div>
          {pref?.is_preferred ? (
            <button className="btn btn-text" onClick={removeDefault} disabled={saving}>
              {t("settings.aiTaskRemoveDefault")}
            </button>
          ) : (
            <button
              className="btn btn-secondary"
              onClick={() => setShowSetDefault(true)}
              disabled={saving || !pref?.entity_id}
              title={pref?.entity_id ? undefined : t("settings.aiTaskConfigureFirst")}
            >
              {t("settings.aiTaskMakeDefault")}
            </button>
          )}
        </div>
      )}

      {showSetDefault && (
        <AiTaskDefaultModal
          entityReady={!!pref?.entity_id}
          currentName={pref?.gen_data_entity_id && !pref?.is_preferred ? (pref?.gen_data_name ?? pref?.gen_data_entity_id) : null}
          onClose={() => setShowSetDefault(false)}
          onConfirm={setDefault}
        />
      )}

      {showDisableConfirm && (
        <AiTaskDisableModal
          onClose={() => setShowDisableConfirm(false)}
          onConfirm={() => { onChange("ai_task_enabled", false); setShowDisableConfirm(false); }}
        />
      )}
    </div>
  );
}

// Confirm making Phoenix MCP the default AI Task data-gen entity. Since HA allows only one,
// this overwrites any prior default, so the modal names it when there is one. Also the
// first-enable prompt: when the entity is not ready yet (no token/provider/model), it
// guides the operator to finish configuring first rather than failing the set.
function AiTaskDefaultModal({
  entityReady,
  currentName,
  onClose,
  onConfirm,
}: {
  entityReady: boolean;
  currentName: string | null;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("settings.aiTaskSetDefaultFailed"));
      setBusy(false);
    }
  }

  return (
    <Modal titleId="ai-task-default-title" onClose={busy ? undefined : onClose}>
      <h3 className="modal-title" id="ai-task-default-title">{t("settings.aiTaskDefaultModalTitle")}</h3>
      <p className="mb-16">
        {tRich("settings.aiTaskDefaultModalBody", { strong: (c) => <strong>{c}</strong> })}
      </p>
      {!entityReady && (
        <div className="banner banner-warn mb-16">
          {t("settings.aiTaskDefaultModalNotReady")}
        </div>
      )}
      {entityReady && currentName && (
        <div className="banner banner-warn mb-16">
          {tRich("settings.aiTaskDefaultModalReplaces", { strong: (c) => <strong>{c}</strong> }, { name: currentName })}
        </div>
      )}
      {error && <div className="banner banner-error" role="alert">{error}</div>}
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={confirm} disabled={busy || !entityReady}>
          {busy ? t("settings.aiTaskSetting") : t("settings.aiTaskSetAsDefault")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={busy}>
          {entityReady ? t("settings.cancel") : t("settings.aiTaskSetUpLater")}
        </button>
      </div>
    </Modal>
  );
}

// Confirm disabling the AI Task when Phoenix MCP is HA's current data-gen default: disabling
// removes the entity, so the default cannot be kept (it would dangle); this makes that
// consequence explicit rather than silent.
function AiTaskDisableModal({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  return (
    <Modal titleId="ai-task-disable-title" onClose={onClose}>
      <h3 className="modal-title" id="ai-task-disable-title">{t("settings.aiTaskDisableTitle")}</h3>
      <p className="mb-16">
        {t("settings.aiTaskDisableBody")}
      </p>
      <div className="modal-actions">
        <button className="btn btn-danger" onClick={onConfirm}>{t("settings.aiTaskDisableConfirm")}</button>
        <button className="btn btn-text" onClick={onClose}>{t("settings.cancel")}</button>
      </div>
    </Modal>
  );
}
