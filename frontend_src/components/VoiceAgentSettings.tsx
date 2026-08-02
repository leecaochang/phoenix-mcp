import { useEffect, useState } from "react";
import type { GlobalSettings, TokenRecord, AgentCliInstance } from "../types";
import { api } from "../api";
import { Modal } from "./Modal";
import { DocsHelpLink } from "./common";
import { t } from "../i18n";

// Voice Agent settings card. Registers Phoenix MCP as a Home Assistant conversation agent
// so "Phoenix MCP" appears in Settings > Voice assistants and runs Phoenix MCP's own model loop on
// the chosen token's scope. Needs a token (scope), an Agent Chat provider account,
// and a model. Independent of the Assist bridge toggle in the card below.
//
// One-click setup: rather than making the operator wire up an Assist pipeline by
// hand, Phoenix MCP can create one pointed at itself (optionally the preferred assistant).
// The tracked pipeline is torn down again when the voice agent is disabled or its
// token/provider goes away (backend); the kill switch leaves it in place (the agent
// stays registered but declines, so the assistant degrades gracefully).
export function VoiceAgentSettings({
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
  const [showSetup, setShowSetup] = useState(false);
  const [showRemove, setShowRemove] = useState(false);
  const [showDisableConfirm, setShowDisableConfirm] = useState(false);

  const enabled = !!settings.voice_agent_enabled;
  const providerId = settings.voice_agent_provider_id ?? "";
  const tokenId = settings.voice_agent_token_id ?? "";
  const model = settings.voice_agent_model ?? "";
  const pipelineId = settings.voice_agent_pipeline_id ?? "";
  const pipelineSupported = settings.voice_agent_pipeline_supported ?? true;
  const noProviders = instances.length === 0;
  const fullyConfigured = !!(enabled && tokenId && providerId && model);

  useEffect(() => {
    api.listTokens().then(setTokens).catch(() => setTokens([]));
    const loadProviders = () =>
      api.getAgentCliProviders().then((r) => setInstances(r.instances)).catch(() => setInstances([]));
    loadProviders();
    // Reload when a provider account is added or removed in the Agent Chat card, so
    // the dropdowns here never show a stale (or deleted) account.
    window.addEventListener("phx-agentcli-providers-changed", loadProviders);
    return () => window.removeEventListener("phx-agentcli-providers-changed", loadProviders);
  }, []);

  // Load the chosen provider's models so the model dropdown can be populated.
  useEffect(() => {
    if (!providerId) { setModels([]); return; }
    let cancelled = false;
    api.getAgentCliModels(providerId)
      .then((r) => { if (!cancelled) setModels(r.models); })
      .catch(() => { if (!cancelled) setModels([]); });
    return () => { cancelled = true; };
  }, [providerId]);

  // Re-pull the parent's settings so a created/removed pipeline id is reflected.
  const refreshSettings = () => window.dispatchEvent(new Event("phx-settings-refresh"));

  function handleEnableToggle(checked: boolean) {
    // Disabling unregisters the agent, so a one-click Assist pipeline pointed at Phoenix MCP
    // would break; Phoenix MCP removes it automatically. Confirm first when one exists so the
    // "my assistant disappeared" consequence is explicit rather than silent.
    if (!checked && pipelineId) {
      setShowDisableConfirm(true);
      return;
    }
    onChange("voice_agent_enabled", checked);
    // Offer the one-click setup the first time the agent is enabled (no pipeline
    // yet). The modal itself handles the not-yet-fully-configured case.
    setShowSetup(checked && !pipelineId && pipelineSupported);
  }

  async function removePipeline() {
    try {
      await api.deleteVoiceAgentPipeline();
    } finally {
      refreshSettings();
    }
  }

  return (
    <div className="card">
      <h3 className="card-header">
        {t("settings.voiceCard")}
        <DocsHelpLink path="connect.html#voice-agent" label={t("settings.voiceCard")} />
      </h3>
      <p className="agentcli-settings-hint">
        {t("settings.voiceIntro")}
      </p>
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.voiceEnableLabel")}</span>
          <small>{t("settings.voiceEnableHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            aria-label={t("settings.voiceEnableLabel")}
            checked={enabled}
            disabled={saving}
            onChange={(e) => handleEnableToggle(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>

      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.token")}</span>
          <small>{t("settings.voiceTokenHelp")}</small>
        </div>
        <select
          aria-label={t("settings.voiceTokenAria")}
          className="input input-auto"
          value={tokenId}
          disabled={saving}
          onChange={(e) => onChange("voice_agent_token_id", e.target.value)}
        >
          <option value="">{t("settings.selectToken")}</option>
          {tokens.map((tok) => (
            <option key={tok.id} value={tok.id}>{tok.name}</option>
          ))}
        </select>
      </div>

      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.providerAccount")}</span>
          <small>{instances.length === 0 ? t("settings.addProviderFirst") : t("settings.voiceProviderHelp")}</small>
        </div>
        <select
          aria-label={t("settings.voiceProviderAria")}
          className="input input-auto"
          value={providerId}
          disabled={saving || noProviders}
          onChange={(e) => onChange("voice_agent_provider_id", e.target.value)}
        >
          <option value="">{t("settings.selectProvider")}</option>
          {instances.map((i) => (
            <option key={i.id} value={i.id}>{i.name}</option>
          ))}
        </select>
      </div>

      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.model")}</span>
          <small>{t("settings.voiceModelHelp")}</small>
        </div>
        <select
          aria-label={t("settings.voiceModelAria")}
          className="input input-auto"
          value={model}
          disabled={saving || !providerId || noProviders}
          onChange={(e) => onChange("voice_agent_model", e.target.value)}
        >
          <option value="">{t("settings.selectModel")}</option>
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      {pipelineSupported && (
        <div className="toggle-row toggle-row-plain settings-toggle-mt">
          <div className="toggle-label">
            <span>{t("settings.voiceAssistSetup")}</span>
            <small>
              {pipelineId
                ? t("settings.voiceAssistSetupCreated")
                : t("settings.voiceAssistSetupNone")}
            </small>
          </div>
          {pipelineId ? (
            <button className="btn btn-text" onClick={() => setShowRemove(true)} disabled={saving}>
              {t("settings.voiceRemoveAssistant")}
            </button>
          ) : (
            <button
              className="btn btn-secondary"
              onClick={() => setShowSetup(true)}
              disabled={saving || !fullyConfigured}
              title={fullyConfigured ? undefined : t("settings.voiceConfigureFirst")}
            >
              {t("settings.voiceSetUpAssistant")}
            </button>
          )}
        </div>
      )}

      {showSetup && (
        <VoiceAgentSetupModal
          fullyConfigured={fullyConfigured}
          onClose={() => setShowSetup(false)}
          onCreated={() => { setShowSetup(false); refreshSettings(); }}
        />
      )}

      {showRemove && (
        <VoiceAgentRemoveModal
          onRemove={removePipeline}
          onClose={() => setShowRemove(false)}
        />
      )}

      {showDisableConfirm && (
        <VoiceAgentDisableModal
          onClose={() => setShowDisableConfirm(false)}
          onConfirm={() => { onChange("voice_agent_enabled", false); setShowDisableConfirm(false); }}
        />
      )}
    </div>
  );
}

// Confirm disabling the voice agent when a one-click Assist pipeline exists: disabling
// unregisters the agent, so Phoenix MCP removes the assistant it added to Home Assistant (a
// kept pipeline would point at a dead agent). Makes that consequence explicit.
function VoiceAgentDisableModal({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  return (
    <Modal titleId="voice-disable-title" onClose={onClose}>
      <h3 className="modal-title" id="voice-disable-title">{t("settings.voiceDisableTitle")}</h3>
      <p className="mb-16">
        {t("settings.voiceDisableBody")}
      </p>
      <div className="modal-actions">
        <button className="btn btn-danger" onClick={onConfirm}>{t("settings.voiceDisableConfirm")}</button>
        <button className="btn btn-text" onClick={onClose}>{t("settings.cancel")}</button>
      </div>
    </Modal>
  );
}

// First-run setup modal: create an Assist pipeline pointed at Phoenix MCP (optionally the
// preferred assistant), or dismiss to wire it up manually. Preferred defaults ON per
// the operator's request; they can turn it off before creating.
function VoiceAgentSetupModal({
  fullyConfigured,
  onClose,
  onCreated,
}: {
  fullyConfigured: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [preferred, setPreferred] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function setupAutomatically() {
    setBusy(true);
    setError(null);
    try {
      await api.createVoiceAgentPipeline(preferred);
      onCreated();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("settings.voiceSetUpFailed"));
      setBusy(false);
    }
  }

  return (
    <Modal titleId="va-setup-title" onClose={busy ? undefined : onClose}>
      <h3 className="modal-title" id="va-setup-title">{t("settings.voiceSetupTitle")}</h3>
      <p className="mb-16">
        {t("settings.voiceSetupBody")}
      </p>
      {!fullyConfigured && (
        <div className="banner banner-warn mb-16">
          {t("settings.voiceSetupNotReady")}
        </div>
      )}
      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span>{t("settings.voicePreferredLabel")}</span>
          <small>{t("settings.voicePreferredHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            aria-label={t("settings.voicePreferredLabel")}
            checked={preferred}
            disabled={busy}
            onChange={(e) => setPreferred(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
      {error && <div className="banner banner-error" role="alert">{error}</div>}
      <div className="modal-actions">
        <button
          className="btn btn-primary"
          onClick={setupAutomatically}
          disabled={busy || !fullyConfigured}
        >
          {busy ? t("settings.voiceSettingUp") : t("settings.voiceSetUpAutomatically")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={busy}>
          {t("settings.voiceSetUpManually")}
        </button>
      </div>
    </Modal>
  );
}

// Confirm removing the Phoenix-created Assist assistant. Removal does not disable the
// voice agent, and the assistant can be set up again from the card.
function VoiceAgentRemoveModal({
  onRemove,
  onClose,
}: {
  onRemove: () => Promise<void>;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onRemove();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("settings.voiceRemoveFailed"));
      setBusy(false);
    }
  }

  return (
    <Modal titleId="va-remove-title" onClose={busy ? undefined : onClose}>
      <h3 className="modal-title" id="va-remove-title">{t("settings.voiceRemoveTitle")}</h3>
      <p className="mb-16">
        {t("settings.voiceRemoveBody")}
      </p>
      {error && <div className="banner banner-error" role="alert">{error}</div>}
      <div className="modal-actions">
        <button className="btn btn-danger" onClick={confirm} disabled={busy}>
          {busy ? t("settings.voiceRemoving") : t("settings.voiceRemoveConfirm")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={busy}>{t("settings.cancel")}</button>
      </div>
    </Modal>
  );
}
