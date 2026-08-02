import React, { useEffect, useRef, useState } from "react";
import type { TokenRecord, CreateTokenBody } from "../types";
import { api } from "../api";
import { copyToClipboard } from "../utils";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";
import { Modal } from "./Modal";
import { ConnectInstructions, CopyCodeBox } from "./ConnectInstructions";

const NAME_REGEX = /^[A-Za-z0-9_\-]{3,32}$/;

interface Props {
  existingNames: string[];
  onCreated: (token: TokenRecord, rawToken: string) => void;
  onClose: () => void;
  onOpenSettings: () => void;
}

type TtlUnit = "minutes" | "hours" | "days" | "weeks" | "none";

function slugify(name: string) {
  return name.toLowerCase().replace(/-/g, "_");
}

function addMinutes(m: number): string {
  const d = new Date(Date.now() + m * 60000);
  return d.toISOString();
}


export function CopyButton({ text, label = t("tokens.copyToClipboard") }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await copyToClipboard(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <>
      <button className="btn btn-primary" onClick={copy}>
        {copied ? t("tokens.copied") : label}
      </button>
      {/* Announce the visual Copy -> Copied! swap to assistive tech. */}
      <span className="sr-only" role="status">{copied ? t("tokens.copiedToClipboard") : ""}</span>
    </>
  );
}

// The raw-token reveal block (amber warning + monospace token). Shared by the
// post-create modal and the onboarding wizard so both look identical. The
// warning text is overridable because the wizard shows the token again on its
// Connect step, so the default "will not be shown again" copy is inaccurate there.
export function RawTokenDisplay({ rawToken, note }: { rawToken: string; note?: React.ReactNode }) {
  return (
    <>
      <div className="amber-block">
        {note ?? <p>{tRich("tokens.rawTokenNote", { strong: (c) => <strong>{c}</strong> })}</p>}
      </div>
      <CopyCodeBox value={rawToken} />
    </>
  );
}

interface PostCreateProps {
  rawToken: string;
  tokenName: string;
  onOpenSettings: () => void;   // "Setup Agent Chat" -> closes this modal, opens Settings
  onDone: () => void;           // finish -> refresh + open the new token's detail page
}

// After a token is created, offer three ways forward instead of dumping the raw
// token immediately: connect an external agent, set up in-panel Agent Chat, or
// just reveal the token to copy.
function PostCreateModal({ rawToken, tokenName, onOpenSettings, onDone }: PostCreateProps) {
  const [view, setView] = useState<"choice" | "connect" | "token">("choice");

  // On view switches the clicked control unmounts; refocus the first control
  // of the new view so keyboard focus never falls out of the dialog.
  const viewsRef = useRef<HTMLDivElement>(null);
  const prevViewRef = useRef(view);
  useEffect(() => {
    if (prevViewRef.current === view) return;
    prevViewRef.current = view;
    viewsRef.current
      ?.querySelector<HTMLElement>("button, [href], input, select, textarea")
      ?.focus();
  }, [view]);

  return (
    <Modal titleId="created-token-title" onClose={onDone}>
      <h3 className="modal-title" id="created-token-title">{t("tokens.tokenCreatedTitle", { name: tokenName })}</h3>
      <div ref={viewsRef} style={{ display: "contents" }}>

      {view === "choice" && (
        <>
          <p className="wizard-sub">{t("tokens.postCreateQuestion")}</p>
          <div className="post-create-choices">
            <button className="post-create-choice" onClick={() => setView("connect")}>
              <span className="post-create-choice-title">{t("tokens.postCreateConnectTitle")}</span>
              <span className="post-create-choice-sub">{t("tokens.postCreateConnectSub")}</span>
            </button>
            <button className="post-create-choice" onClick={onOpenSettings}>
              <span className="post-create-choice-title">{t("tokens.postCreateChatTitle")}</span>
              <span className="post-create-choice-sub">{t("tokens.postCreateChatSub")}</span>
            </button>
            <button className="post-create-choice" onClick={() => setView("token")}>
              <span className="post-create-choice-title">{t("tokens.postCreateShowTitle")}</span>
              <span className="post-create-choice-sub">{t("tokens.postCreateShowSub")}</span>
            </button>
          </div>
          <div className="modal-actions">
            <button className="btn btn-text" onClick={onDone}>{t("common.close")}</button>
          </div>
        </>
      )}

      {view === "connect" && (
        <>
          <button className="btn btn-text btn-sm" onClick={() => setView("choice")}><span aria-hidden="true">&larr;</span> {t("tokens.back")}</button>
          <ConnectInstructions token={rawToken} tokenName={tokenName} />
          <div className="modal-actions">
            <button className="btn btn-primary" onClick={onDone}>{t("tokens.done")}</button>
          </div>
        </>
      )}

      {view === "token" && (
        <>
          <button className="btn btn-text btn-sm" onClick={() => setView("choice")}><span aria-hidden="true">&larr;</span> {t("tokens.back")}</button>
          <RawTokenDisplay rawToken={rawToken} />
          <div className="banner banner-info">
            {t("tokens.postCreateReplaceNote")}
          </div>
          <div className="modal-actions">
            <button className="btn btn-primary" onClick={onDone}>{t("tokens.done")}</button>
          </div>
        </>
      )}
      </div>
    </Modal>
  );
}

export function TokenCreateModal({ existingNames, onCreated, onClose, onOpenSettings }: Props) {
  const [name, setName] = useState("");
  const [ttlUnit, setTtlUnit] = useState<TtlUnit>("none");
  const [ttlValue, setTtlValue] = useState("24");
  const [passThrough, setPassThrough] = useState(false);
  const [ptConfirmed, setPtConfirmed] = useState(false);
  const [rateLimitRequests, setRateLimitRequests] = useState("60");
  const [rateLimitBurst, setRateLimitBurst] = useState("10");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<{ record: TokenRecord; raw: string } | null>(null);

  const nameError = (() => {
    if (!name) return null;
    if (!NAME_REGEX.test(name)) return t("tokens.nameFormatError");
    const slug = slugify(name);
    if (existingNames.some((n) => slugify(n) === slug)) return t("tokens.nameDuplicateError");
    return null;
  })();

  const reqNum = parseInt(rateLimitRequests, 10);
  const burstDisabled = isNaN(reqNum) || reqNum === 0;

  const canSubmit =
    name.length >= 3 &&
    !nameError &&
    (!passThrough || ptConfirmed) &&
    !saving;

  async function submit() {
    setSaving(true);
    setError(null);
    try {
      let expiresAt: string | undefined;
      if (ttlUnit !== "none") {
        const n = parseInt(ttlValue, 10);
        const minutes =
          ttlUnit === "minutes" ? n :
          ttlUnit === "hours" ? n * 60 :
          ttlUnit === "days" ? n * 60 * 24 :
          n * 60 * 24 * 7;
        expiresAt = addMinutes(minutes);
      }
      const burstNum = burstDisabled ? 0 : parseInt(rateLimitBurst, 10);
      const body: CreateTokenBody = {
        name,
        expires_at: expiresAt,
        pass_through: passThrough,
        confirm_pass_through: passThrough ? true : undefined,
        rate_limit_requests: parseInt(rateLimitRequests, 10) || 0,
        rate_limit_burst: burstNum,
      };
      const resp = await api.createToken(body);
      const { token: rawToken, ...record } = resp;
      setCreatedToken({ record, raw: rawToken });
      // The caller's onRefresh covers the panel shell, but the floating chat
      // window is a separate bundle that only learns about new tokens here.
      window.dispatchEvent(new CustomEvent("phx-tokens-changed"));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (createdToken) {
    return (
      <PostCreateModal
        rawToken={createdToken.raw}
        tokenName={createdToken.record.name}
        onOpenSettings={() => { onClose(); onOpenSettings(); }}
        onDone={() => {
          onCreated(createdToken.record, createdToken.raw);
          onClose();
        }}
      />
    );
  }

  return (
    <Modal titleId="create-token-title" onClose={saving ? undefined : onClose}>
      <h3 className="modal-title" id="create-token-title">{t("tokens.createToken")}</h3>

      <div className="field">
        <label htmlFor="token-name-input">{t("tokens.nameLabel")}</label>
          <input
            id="token-name-input"
            className={`input${nameError ? " error" : ""}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("tokens.namePlaceholder")}
            maxLength={32}
            autoFocus
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? "token-name-error" : undefined}
          />
          {nameError && <span className="field-error" id="token-name-error" role="alert">{nameError}</span>}
        </div>

        <div className="field">
          <label htmlFor="token-expiry-unit">{t("tokens.expiryLabel")}</label>
          <div className="token-create-expiry-row">
            <select
              id="token-expiry-unit"
              className="input input-auto"
              value={ttlUnit}
              onChange={(e) => setTtlUnit(e.target.value as TtlUnit)}
            >
              <option value="none">{t("tokens.expiryNone")}</option>
              <option value="minutes">{t("tokens.expiryMinutes")}</option>
              <option value="hours">{t("tokens.expiryHours")}</option>
              <option value="days">{t("tokens.expiryDays")}</option>
              <option value="weeks">{t("tokens.expiryWeeks")}</option>
            </select>
            {ttlUnit !== "none" && (
              <input
                aria-label={t("tokens.expiryAmountAria")}
                className="input token-create-expiry-value"
                type="number"
                min={1}
                value={ttlValue}
                onChange={(e) => setTtlValue(e.target.value)}
              />
            )}
          </div>
        </div>

        <div className="toggle-row">
          <div className="toggle-label">
            <span>{t("tokens.passThroughMode")}</span>
            <small>{t("tokens.passThroughModeHelp")}</small>
          </div>
          <label className="toggle-switch">
            <input
              type="checkbox"
              aria-label={t("tokens.passThroughMode")}
              checked={passThrough}
              onChange={(e) => { setPassThrough(e.target.checked); setPtConfirmed(false); }}
            />
            <span className="toggle-switch-track" />
          </label>
        </div>

        {passThrough ? (
          <div className="amber-block">
            <p>
              {tRich("tokens.createPtWarning", { strong: (c) => <strong>{c}</strong> })}
            </p>
            <div className="toggle-row mt-10">
              <div className="toggle-label"><span>{t("tokens.createPtUnderstand")}</span></div>
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  aria-label={t("tokens.createPtUnderstand")}
                  checked={ptConfirmed}
                  onChange={(e) => setPtConfirmed(e.target.checked)}
                />
                <span className="toggle-switch-track" />
              </label>
            </div>
          </div>
        ) : (
          <div className="token-create-rate-section">
            <div className="token-create-rate-fields">
              <div className="field token-create-rate-field">
                <label htmlFor="create-rate-requests">{t("tokens.rateRequestsLabel")}</label>
                <input
                  id="create-rate-requests"
                  className="input"
                  type="number"
                  min={0}
                  value={rateLimitRequests}
                  onChange={(e) => setRateLimitRequests(e.target.value)}
                />
              </div>
              <div className="field token-create-rate-field">
                <label htmlFor="create-rate-burst">{t("tokens.rateBurstLabel")}</label>
                <input
                  id="create-rate-burst"
                  className="input"
                  type="number"
                  min={0}
                  value={burstDisabled ? "0" : rateLimitBurst}
                  disabled={burstDisabled}
                  onChange={(e) => setRateLimitBurst(e.target.value)}
                />
              </div>
            </div>
          </div>
        )}

        {error && <div className="banner banner-error mt-12">{error}</div>}

      <div className="modal-actions">
        <button className="btn btn-primary" onClick={submit} disabled={!canSubmit}>
          {saving ? t("tokens.creating") : t("tokens.create")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={saving}>{t("tokens.cancel")}</button>
      </div>
    </Modal>
  );
}
