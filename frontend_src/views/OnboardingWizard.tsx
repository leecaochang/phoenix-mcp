import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentCliInstance, AgentCliProviderType, CreateTokenBody, EntityTree as EntityTreeData, Persona, PermissionTree } from "../types";
import { api, ApiError } from "../api";
import { PersonaPicker } from "../components/PersonaPicker";
import { EntityTree } from "../components/EntityTree";
import { CopyButton } from "../components/TokenCreateModal";
import { ConnectInstructions } from "../components/ConnectInstructions";
import { ProviderAddForm } from "../components/ProviderAddForm";
import { PERSONA_CAP_DEFAULTS } from "../personas";
import { buildTestPrompt, firstGreenTarget } from "../wizard_helpers";
import { patchDurable as patchAgentCliDurable } from "../utils/agentcli_state";
import { ErrorMsg } from "../index";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";

const NAME_REGEX = /^[A-Za-z0-9_\-]{3,32}$/;


// Steps 0-3 are shared; step 3 branches. "chat" runs the agent inside Home
// Assistant via Agent Chat (the preferred path); "app" connects an external
// MCP app (the pre-existing Connect/Test flow).
type Branch = "app" | "chat";

type TtlUnit = "minutes" | "hours" | "days" | "weeks" | "none";

function addMinutes(m: number): string {
  return new Date(Date.now() + m * 60000).toISOString();
}

interface Props {
  onCancel: () => void;
  onFinish: (tokenId: string) => void;
}

export function OnboardingWizard({ onCancel, onFinish }: Props) {
  const [step, setStep] = useState(0);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [persona, setPersona] = useState<Persona | null>("new_user");
  const [name, setName] = useState("test_token");
  const [ttlUnit, setTtlUnit] = useState<TtlUnit>("none");
  const [ttlValue, setTtlValue] = useState("24");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [rawToken, setRawToken] = useState<string | null>(null);
  const [tokenId, setTokenId] = useState<string | null>(null);
  const [permissions, setPermissions] = useState<PermissionTree>({ domains: {}, devices: {}, entities: {} });

  const [entityTree, setEntityTree] = useState<EntityTreeData | null>(null);
  const [mesaEnforced, setMesaEnforced] = useState(false);
  const [showAllDomains, setShowAllDomains] = useState(false);

  const [connected, setConnected] = useState(false);

  useEffect(() => {
    api.getEntityTree().then(setEntityTree).catch(() => null);
    api.getSettings().then((s) => setMesaEnforced(s.mesa_mode === "enforced")).catch(() => null);
  }, []);

  // Keyboard focus lands on the new step's heading after Next/Back; the
  // clicked button unmounts with the old step, which would otherwise drop
  // focus to <body>. Skipped on first render (initial focus is fine).
  const bodyRef = useRef<HTMLDivElement>(null);
  const prevStepRef = useRef(step);
  useEffect(() => {
    if (prevStepRef.current === step) return;
    prevStepRef.current = step;
    const heading = bodyRef.current?.querySelector<HTMLElement>(".wizard-title");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus();
    }
  }, [step]);

  const grantedEntityId = firstGreenTarget(permissions, entityTree);
  const grantedFriendlyName = (() => {
    if (!grantedEntityId) return "";
    if (!entityTree) return grantedEntityId;
    const domain = grantedEntityId.split(".")[0];
    return entityTree[domain]?.entity_details[grantedEntityId]?.friendly_name ?? grantedEntityId;
  })();

  const hasLights = !!(entityTree && entityTree["light"] && Object.keys(entityTree["light"].entity_details).length > 0);

  async function createAndAdvance() {
    if (!persona) return;
    setSaving(true);
    setError(null);
    try {
      const caps = PERSONA_CAP_DEFAULTS[persona];
      const patchBody: Record<string, unknown> = { persona, ...(caps || {}) };
      if (!tokenId) {
        if (name && !NAME_REGEX.test(name)) {
          setError(t("wizard.nameError"));
          return;
        }
        let expiresAt: string | undefined;
        if (ttlUnit !== "none") {
          const n = parseInt(ttlValue, 10);
          const minutes = ttlUnit === "minutes" ? n : ttlUnit === "hours" ? n * 60 : ttlUnit === "days" ? n * 1440 : n * 10080;
          expiresAt = addMinutes(minutes);
        }
        const body: CreateTokenBody = { name, expires_at: expiresAt, pass_through: false };
        const resp = await api.createToken(body);
        const { token: raw, ...record } = resp;
        // Stash the irreversible bits BEFORE the persona patch so a patch
        // failure leaves a recoverable (retry-only-the-patch) state.
        setRawToken(raw);
        setTokenId(record.id);
        setPermissions(record.permissions);
        // Creation changes the token list every host caches: the panel shell
        // gates its header "Agent Chat" button on a non-empty list and hands
        // the panel-hosted chat window its tokens prop, and the floating
        // window refetches on this event. Without it a token made here is
        // invisible to both until a page reload.
        window.dispatchEvent(new CustomEvent("phx-tokens-changed"));
        await api.patchToken(record.id, patchBody);
      } else {
        // Token already created (back-navigation): re-apply persona only.
        await api.patchToken(tokenId, patchBody);
      }
      setStep(2);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("wizard.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  // Poll for connection while on the Test step.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const poll = useCallback(async () => {
    if (!tokenId) return;
    try {
      const c = await api.getTokenConnection(tokenId);
      if (c.request_count > 0) setConnected(true);
    } catch {
      // transient; keep polling
    }
  }, [tokenId]);

  useEffect(() => {
    if (step !== 5 || branch !== "app" || connected) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [step, branch, connected, poll]);

  // Agent Chat branch: the wizard's job is done the moment the user sends
  // their first prompt through the window it opened; close to the token page.
  useEffect(() => {
    if (step !== 5 || branch !== "chat") return;
    const done = () => { if (tokenId) onFinish(tokenId); };
    window.addEventListener("phx-agentcli-sent", done);
    return () => window.removeEventListener("phx-agentcli-sent", done);
  }, [step, branch, tokenId, onFinish]);

  // Open Agent Chat on the new token with the chosen account/model preselected
  // and the test prompt pre-typed. Closing any already-open global window first
  // forces a remount, so the freshly patched durable selections are picked up.
  const launchAgentChat = useCallback((instanceId: string, model: string) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__phxAgentChat?.close?.();
    patchAgentCliDurable({ instanceId, model, prefill: t("wizard.promptChat") });
    window.dispatchEvent(new CustomEvent("phx-open-agentcli", { detail: { tokenId } }));
    setStep(5);
  }, [tokenId]);

  const prompts = buildTestPrompt(grantedFriendlyName || t("wizard.yourDevice"), mesaEnforced);

  const stepLabels = branch === "chat"
    ? [t("wizard.stepPersona"), t("wizard.stepToken"), t("wizard.stepAccess"), t("wizard.stepChoose"), t("wizard.stepProvider"), t("wizard.stepTry")]
    : [t("wizard.stepPersona"), t("wizard.stepToken"), t("wizard.stepAccess"), t("wizard.stepChoose"), t("wizard.stepConnect"), t("wizard.stepTest"), t("wizard.stepDone")];

  function Stepper() {
    return (
      <ol className="wizard-stepper" aria-label={t("wizard.stepperAria")}>
        {stepLabels.map((label, i) => (
          <li
            key={label}
            className={`wizard-step${i === step ? " wizard-step-active" : ""}${i < step ? " wizard-step-done" : ""}`}
            aria-current={i === step ? "step" : undefined}
          >
            <span className="wizard-step-num">{i + 1}</span>
            <span className="wizard-step-label">{label}</span>
          </li>
        ))}
      </ol>
    );
  }

  function renderBody() {
    switch (step) {
      case 0:
        return (
          <>
            <h3 className="wizard-title">{t("wizard.connectTitle")}</h3>
            <p className="wizard-sub">{tRich("wizard.personaSub", { strong: (c) => <strong>{c}</strong> })}</p>
            <PersonaPicker selected={persona} onSelect={setPersona} />
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={onCancel}>{t("common.cancel")}</button>
              <button className="btn btn-primary" disabled={!persona} onClick={() => setStep(1)}>{t("wizard.next")}</button>
            </div>
          </>
        );
      case 1:
        return (
          <>
            <h3 className="wizard-title">{t("wizard.tokenTitle")}</h3>
            <p className="wizard-sub">{t("wizard.tokenSub")}</p>
            <div className="field">
              <label htmlFor="wiz-name">{t("wizard.nameLabel")}</label>
              <input id="wiz-name" className="input" value={name} disabled={!!tokenId}
                maxLength={32} onChange={(e) => setName(e.target.value)} placeholder="test_token" />
            </div>
            <div className="field">
              <label htmlFor="wiz-expiry-unit">{t("wizard.expiryLabel")}</label>
              <div className="token-create-expiry-row">
                <select id="wiz-expiry-unit" className="input input-auto" value={ttlUnit} disabled={!!tokenId}
                  onChange={(e) => setTtlUnit(e.target.value as TtlUnit)}>
                  <option value="none">{t("wizard.expiryNone")}</option>
                  <option value="minutes">{t("wizard.expiryMinutes")}</option>
                  <option value="hours">{t("wizard.expiryHours")}</option>
                  <option value="days">{t("wizard.expiryDays")}</option>
                  <option value="weeks">{t("wizard.expiryWeeks")}</option>
                </select>
                {ttlUnit !== "none" && (
                  <input className="input token-create-expiry-value" type="number" min={1}
                    aria-label={t("wizard.expiryAmountAria")}
                    value={ttlValue} disabled={!!tokenId} onChange={(e) => setTtlValue(e.target.value)} />
                )}
              </div>
            </div>
            {tokenId && <div className="banner banner-info">{t("wizard.tokenCreatedBanner")}</div>}
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={() => setStep(0)}>{t("wizard.back")}</button>
              <button className="btn btn-primary" disabled={saving} onClick={createAndAdvance}>
                {saving ? t("wizard.working") : tokenId ? t("wizard.next") : t("wizard.createToken")}
              </button>
            </div>
          </>
        );
      case 2:
        return (
          <>
            <h3 className="wizard-title">{t("wizard.accessTitle")}</h3>
            <p className="wizard-sub">{tRich("wizard.accessSub", { code: (c) => <code>{c}</code>, strong: (c) => <strong>{c}</strong> })}</p>
            {!hasLights && !showAllDomains && (
              <div className="banner banner-warn">
                {t("wizard.noLights")}{" "}
                <button className="btn btn-text btn-sm" onClick={() => setShowAllDomains(true)}>{t("wizard.showAllDevices")}</button>
              </div>
            )}
            {tokenId && (
              <EntityTree
                tokenId={tokenId}
                permissions={permissions}
                onPermissionsChange={setPermissions}
                domainAllowlist={showAllDomains ? undefined : ["light"]}
              />
            )}
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={() => setStep(1)}>{t("wizard.back")}</button>
              <button className="btn btn-primary" disabled={!grantedEntityId} onClick={() => setStep(3)}>{t("wizard.next")}</button>
            </div>
          </>
        );
      case 3:
        return (
          <>
            <h3 className="wizard-title">{t("wizard.chooseTitle")}</h3>
            <p className="wizard-sub">{t("wizard.chooseSub")}</p>
            <div className="wizard-choice-row">
              <button type="button" className="wizard-choice" onClick={() => { setBranch("chat"); setStep(4); }}>
                <strong>{t("wizard.choiceChatTitle")}</strong>
                <small>{t("wizard.choiceChatBody")}</small>
              </button>
              <button type="button" className="wizard-choice" onClick={() => { setBranch("app"); setStep(4); }}>
                <strong>{t("wizard.choiceAppTitle")}</strong>
                <small>{t("wizard.choiceAppBody")}</small>
              </button>
            </div>
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={() => setStep(2)}>{t("wizard.back")}</button>
            </div>
          </>
        );
      case 4:
        if (branch === "chat") {
          return (
            <WizardProviderSetup
              onBack={() => setStep(3)}
              onTryNow={launchAgentChat}
            />
          );
        }
        return (
          <>
            <h3 className="wizard-title">{t("wizard.connectAgentTitle")}</h3>
            {rawToken && <ConnectInstructions token={rawToken} tokenName={name} />}
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={() => setStep(3)}>{t("wizard.back")}</button>
              <button className="btn btn-ghost" onClick={() => setStep(6)}>{t("wizard.connectLater")}</button>
              <button className="btn btn-primary" onClick={() => setStep(5)}>{t("wizard.testConnection")}</button>
            </div>
          </>
        );
      case 5:
        if (branch === "chat") {
          return (
            <>
              <h3 className="wizard-title">{t("wizard.chatReadyTitle")}</h3>
              <p className="wizard-sub">{tRich("wizard.chatReadySub", { strong: (c) => <strong>{c}</strong> })}</p>
              <div className="wizard-actions">
                <button className="btn btn-primary" onClick={() => tokenId && onFinish(tokenId)}>{t("wizard.goToToken")}</button>
              </div>
            </>
          );
        }
        return (
          <>
            <h3 className="wizard-title">{t("wizard.testTitle")}</h3>
            <p className="wizard-sub">{t("wizard.testSub")}</p>
            <div className="connect-field">
              <span className="connect-field-label">{t("wizard.tryLabel")}</span>
              <code className="connect-field-value">{prompts.read}</code>
              <CopyButton text={prompts.read} label={t("common.copy")} />
            </div>
            {prompts.action && (
              <div className="connect-field">
                <span className="connect-field-label">{t("wizard.thenLabel")}</span>
                <code className="connect-field-value">{prompts.action}</code>
                <CopyButton text={prompts.action} label={t("common.copy")} />
              </div>
            )}
            {mesaEnforced && (
              <p className="wizard-hint">{t("wizard.mesaHint")}</p>
            )}
            <div className={`wizard-connect-status${connected ? " wizard-connect-status-ok" : ""}`}>
              {connected ? t("wizard.connected") : t("wizard.waitingForAgent")}
            </div>
            <div className="wizard-actions">
              <button className="btn btn-text" onClick={() => setStep(4)}>{t("wizard.back")}</button>
              <button className="btn btn-primary" disabled={!connected} onClick={() => setStep(6)}>{t("wizard.next")}</button>
            </div>
          </>
        );
      default:
        return (
          <>
            <h3 className="wizard-title">{t("wizard.doneTitle")}</h3>
            <p className="wizard-sub">{tRich("wizard.doneSub", { code: (c) => <code>{c}</code> }, { name })}</p>
            <div className="wizard-actions">
              <button className="btn btn-primary" onClick={() => tokenId && onFinish(tokenId)}>{t("wizard.goToToken")}</button>
            </div>
          </>
        );
    }
  }

  return (
    <div className="view-root wizard-root">
      <div className="wizard-center">
        <Stepper />
        {error && <ErrorMsg msg={error} />}
        <div className="card wizard-body" ref={bodyRef}>{renderBody()}</div>
      </div>
    </div>
  );
}

// The Agent Chat branch's provider step: pick an already-configured account, or
// add a new one (kind + API key / Ollama URL, validated via the store-nothing
// probe, exactly like the Settings > Agent Chat card). "Try now" commits a
// validated new account (or uses the selected existing one) and hands its
// instance id + model to the wizard to launch the chat window.
export function WizardProviderSetup({ onBack, onTryNow }: {
  onBack: () => void;
  onTryNow: (instanceId: string, model: string) => void;
}) {
  const [instances, setInstances] = useState<AgentCliInstance[] | null>(null);
  const [providerTypes, setProviderTypes] = useState<AgentCliProviderType[]>([]);
  const [selected, setSelected] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.getAgentCliProviders()
      .then((r) => {
        setInstances(r.instances);
        setProviderTypes(r.provider_types ?? []);
        if (r.instances.length > 0) setSelected(r.instances[0].id);
      })
      .catch(() => { setInstances([]); setProviderTypes([]); });
  }, []);

  const tryExisting = async () => {
    setBusy(true);
    setErr(null);
    try {
      const inst = instances?.find((i) => i.id === selected);
      onTryNow(selected, inst?.model ?? "");
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : t("wizard.providerSaveFailed"));
      setBusy(false);
    }
  };

  const providerCreated = async (instance: AgentCliInstance, probe: boolean) => {
    if (probe) {
      try { await api.probeAgentCliCapabilities(instance.id); } catch { /* advisory */ }
    }
    window.dispatchEvent(new CustomEvent("phx-agentcli-providers-changed"));
    onTryNow(instance.id, instance.model ?? "");
  };

  return (
    <>
      <h3 className="wizard-title">{t("wizard.providerTitle")}</h3>
      <p className="wizard-sub">
        {t("wizard.providerIntro")}{" "}
        {instances && instances.length > 0
          ? t("wizard.providerPickExisting")
          : t("wizard.providerAddFirst")}
      </p>

      {instances && instances.length > 0 && (
        <div className="field">
          <label htmlFor="wiz-provider-account">{t("wizard.providerAccount")}</label>
          <select id="wiz-provider-account" className="input" value={selected}
                  disabled={busy || adding}
                  onChange={(e) => setSelected(e.target.value)}>
            {instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
          </select>
        </div>
      )}

      <ProviderAddForm
        providerTypes={providerTypes}
        disabled={busy}
        completeLabel={t("wizard.tryNow")}
        addingLabelKey="wizard.addingProvider"
        onActiveChange={setAdding}
        onCreated={providerCreated}
      />
      {!adding && err && <div className="banner banner-error" role="alert">{err}</div>}

      <div className="wizard-actions">
        <button className="btn btn-text" disabled={busy} onClick={onBack}>{t("wizard.back")}</button>
        {!adding && (
          <button className="btn btn-primary" disabled={!selected || busy} onClick={() => void tryExisting()}>
            {busy ? t("wizard.working") : t("wizard.tryNow")}
          </button>
        )}
      </div>
    </>
  );
}
