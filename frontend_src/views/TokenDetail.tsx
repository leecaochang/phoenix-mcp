import React, { useState, useEffect, useCallback, useRef } from "react";
import type { TokenRecord, PatchTokenBody } from "../types";
import PHOENIX_ICON from "../../custom_components/phoenix_mcp/brand/icon.png";
import { api } from "../api";
import { Loading, ErrorMsg } from "../index";
import { formatDateTime, tokenStatus, tokenStatusLabel } from "../utils";
import type { LinkScope } from "../components/MesaProfileLink";
import { Modal } from "../components/Modal";
import { RawTokenDisplay } from "../components/TokenCreateModal";
import { ConnectInstructions } from "../components/ConnectInstructions";
import { CapabilityMatrix, CAP_NAMES } from "../components/CapabilityMatrix";
import type { EsphomeAvailability } from "../components/CapabilityMatrix";
import { PersonaPicker } from "../components/PersonaPicker";
import { PERSONAS } from "../personas";
import { RateLimitConfig } from "../components/RateLimitConfig";
import { CollapsibleCard } from "../components/CollapsibleCard";
import { PassThroughNotice } from "../components/PassThroughNotice";
import { EntityTree } from "../components/EntityTree";
import { PermissionSummary } from "../components/PermissionSummary";
import { PermissionSimulator } from "../components/PermissionSimulator";
import { SelectByPicker } from "../components/SelectByPicker";
import { DocsHelpLink } from "../components/common";
import { ProfileEditor } from "./MesaView";
import { localeDateTime, t } from "../i18n";
import { tRich } from "../i18n/rich";

// Default inline-approval-wait duration shown when a token has none set;
// mirrors const.py DEFAULT_CONFIRM_INLINE_WAIT_SECONDS. The UI exposes only the
// duration (options span the 30-180 range); 0 (off, for unattended agents) is
// API-only, never set from here.

// Whether a token change alters which tools the MCP client is shown (tools/list),
// which is the only kind of change that requires a connected agent to reconnect.
// A capability only changes the announced set when it crosses the deny boundary: a
// cap-tied tool is announced whenever its cap is not "deny", so allow<->confirm
// (which only changes per-request gating, enforced live) does NOT need a reconnect.
// pass_through and announce_all_tools change the announced set wholesale. Persona
// changes surface here as capability changes.
function toolGatingChanged(a: TokenRecord, b: TokenRecord): boolean {
  const capCrossedDeny = CAP_NAMES.some((c) => (a[c] === "deny") !== (b[c] === "deny"));
  return capCrossedDeny || a.pass_through !== b.pass_through || a.announce_all_tools !== b.announce_all_tools;
}

// The destructive actions on this page, each behind its own confirm modal.
// Exactly one can be in flight at a time; see the `action` state below.
type TokenAction = "revoke" | "rotate" | "passThrough" | "clearPerms";

interface Props {
  tokenId: string;
  onBack: () => void;
  onRefresh?: () => void;
  // settings.token_presets_enabled, threaded from the panel shell; the Presets
  // card renders only while the feature toggle is on.
  presetsEnabled?: boolean;
  // Which ESPHome surfaces exist on this system, threaded from the panel shell.
  // Null while settings are still loading; the ESPHome controls simply show no
  // note until it arrives.
  esphome?: EsphomeAvailability | null;
}


interface ConfirmModalProps {
  title: string;
  body: React.ReactNode;
  checkLabel: string;
  confirmLabel: string;
  confirmClass: string;
  loading: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

function ConfirmModal({ title, body, checkLabel, confirmLabel, confirmClass, loading, onConfirm, onClose }: ConfirmModalProps) {
  const [checked, setChecked] = useState(false);
  const titleId = `confirm-modal-${title.replace(/\s+/g, "-").toLowerCase()}`;
  return (
    <Modal titleId={titleId} onClose={loading ? undefined : onClose}>
      <h3 className="modal-title" id={titleId}>{title}</h3>
      {body}
      <div className="toggle-row mt-12" style={{ borderTop: "1px solid var(--phx-border)", paddingTop: 12 }}>
        <div className="toggle-label"><span>{checkLabel}</span></div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            aria-label={checkLabel}
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
      <div className="modal-actions">
        <button className={`btn ${confirmClass}`} onClick={onConfirm} disabled={!checked || loading}>
          {loading ? t("tokens.pleaseWait") : confirmLabel}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={loading}>{t("tokens.cancel")}</button>
      </div>
    </Modal>
  );
}

function RotatedTokenModal({ rawToken, tokenName, onClose }: { rawToken: string; tokenName: string; onClose: () => void }) {
  const [closeEnabled, setCloseEnabled] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    timerRef.current = setTimeout(() => setCloseEnabled(true), 3000);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, []);

  return (
    <Modal titleId="rotated-token-title" onClose={closeEnabled ? onClose : undefined}>
      <h3 className="modal-title" id="rotated-token-title">{t("tokens.tokenRotatedTitle", { name: tokenName })}</h3>
      <RawTokenDisplay
        rawToken={rawToken}
        note={<p>{tRich("tokens.rotatedNote", { strong: (c) => <strong>{c}</strong> })}</p>}
      />
      <div className="banner banner-info">
        {t("tokens.rotatedBanner")}
      </div>
      <details className="connect-details">
        <summary>{t("tokens.helpConnect")}</summary>
        <ConnectInstructions token={rawToken} tokenName={tokenName} />
      </details>
      <div className="modal-actions">
        <button
          className="btn btn-text"
          onClick={onClose}
          disabled={!closeEnabled}
          title={closeEnabled ? undefined : t("tokens.waitBeforeClose")}
        >
          {closeEnabled ? t("common.close") : t("tokens.closeCountdown")}
        </button>
      </div>
    </Modal>
  );
}

export function ToolAnnouncementToggle({ token, onUpdate }: { token: TokenRecord; onUpdate: (t: TokenRecord) => void }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function patch(body: PatchTokenBody) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.patchToken(token.id, body);
      onUpdate(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.updateFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      {error && <div className="banner banner-error mb-8">{error}</div>}
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("tokens.announceAllLabel")}</span>
          <small>{t("tokens.announceAllHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            aria-label={t("tokens.announceAllLabel")}
            checked={!!token.announce_all_tools}
            disabled={saving}
            onChange={(e) => patch({ announce_all_tools: e.target.checked })}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("tokens.assistExposureLabel")}</span>
          <small>{t("tokens.assistExposureHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            aria-label={t("tokens.assistExposureLabel")}
            checked={!!token.use_assist_exposure}
            disabled={saving || !token.pass_through}
            onChange={(e) => patch({ use_assist_exposure: e.target.checked })}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("tokens.inlineWaitLabel")}</span>
          <small>
            {t("tokens.inlineWaitHelp")}
            {(token.confirm_inline_wait_seconds ?? 0) === 0 && (
              <> <em> {t("tokens.inlineWaitDisabled")}</em></>
            )}
          </small>
        </div>
        <select
          className="input input-auto"
          aria-label={t("tokens.inlineWaitAria")}
          value={token.confirm_inline_wait_seconds ?? 0}
          disabled={saving}
          onChange={(e) => patch({ confirm_inline_wait_seconds: Number(e.target.value) })}
        >
          <option value={0}>{t("tokens.inlineWaitOff")}</option>
          {inlineWaitOptions(token.confirm_inline_wait_seconds ?? 0).map((s) => (
            <option key={s} value={s}>{t("tokens.secondsOption", { s })}</option>
          ))}
        </select>
      </div>
    </>
  );
}

// Duration choices for the inline-wait select: fixed steps across the allowed
// range, plus the token's own value if the API set an off-step one (so it still
// displays correctly). 0 (off) is rendered separately as the first option and is
// the DEFAULT: a confirm-gated call returns pending immediately so approvals
// stage in the queue, and an agent that wants the outcome calls wait_for_approval.
// Turning the wait on makes a single action block, which is a preference rather
// than the thing every caller pays for.
function inlineWaitOptions(current: number): number[] {
  const base = [30, 45, 60, 90, 120, 150, 180];
  const withCurrent = current > 0 && !base.includes(current) ? [...base, current] : base;
  return [...new Set(withCurrent)].sort((a, b) => a - b);
}

// ---------------------------------------------------------------------------
// Token settings presets (workspace model). A preset is a named snapshot of the
// token's full settings; the ACTIVE preset absorbs live edits when the admin
// switches to another preset, and applying the active preset reverts to its
// saved state. Enforcement never reads presets; apply routes through the same
// backend paths as manual edits.
// ---------------------------------------------------------------------------

const MAX_PRESETS = 8; // mirrors const.py MAX_PRESETS_PER_TOKEN

// Normalized comparable view of a settings snapshot, shared by the token's
// live state and a stored preset so dirty detection and diffs line up.
interface SettingsSnap {
  caps: Record<string, string>;
  pass_through: boolean;
  use_assist_exposure: boolean;
  announce_all_tools: boolean;
  confirm_inline_wait_seconds: number;
  rate_limit_requests: number;
  rate_limit_burst: number;
  permissions: import("../types").PermissionTree;
}

function snapOfToken(tok: TokenRecord): SettingsSnap {
  return {
    caps: Object.fromEntries(CAP_NAMES.map((c) => [c, tok[c]])),
    pass_through: tok.pass_through,
    use_assist_exposure: tok.pass_through ? (tok.use_assist_exposure ?? false) : false,
    announce_all_tools: tok.announce_all_tools ?? false,
    confirm_inline_wait_seconds: tok.confirm_inline_wait_seconds ?? 0,
    rate_limit_requests: tok.rate_limit_requests,
    rate_limit_burst: tok.rate_limit_burst,
    permissions: tok.permissions,
  };
}

function snapOfPreset(p: import("../types").TokenPreset): SettingsSnap {
  return {
    caps: Object.fromEntries(CAP_NAMES.map((c) => [c, p.caps[c] ?? "deny"])),
    pass_through: p.pass_through,
    use_assist_exposure: p.pass_through ? p.use_assist_exposure : false,
    announce_all_tools: p.announce_all_tools,
    confirm_inline_wait_seconds: p.confirm_inline_wait_seconds ?? 0,
    rate_limit_requests: p.rate_limit_requests,
    rate_limit_burst: p.rate_limit_burst,
    permissions: p.permissions,
  };
}

function nodeSig(n?: import("../types").PermissionNode): string {
  return n ? `${n.state}|${n.hint ?? ""}` : "GREY|";
}

// Human-readable list of what changes going FROM one snapshot TO another.
// Empty means the two are identical (used as the dirty check).
function snapDiffLines(from: SettingsSnap, to: SettingsSnap): string[] {
  const lines: string[] = [];
  for (const c of CAP_NAMES) {
    if (from.caps[c] !== to.caps[c]) lines.push(`${c}: ${from.caps[c]} -> ${to.caps[c]}`);
  }
  if (from.pass_through !== to.pass_through) {
    lines.push(t("tokens.diffPassThrough", {
      from: from.pass_through ? t("tokens.on") : t("tokens.off"),
      to: to.pass_through ? t("tokens.on") : t("tokens.off"),
    }));
  }
  if (from.use_assist_exposure !== to.use_assist_exposure) {
    lines.push(t("tokens.diffAssistExposure", {
      from: from.use_assist_exposure ? t("tokens.on") : t("tokens.off"),
      to: to.use_assist_exposure ? t("tokens.on") : t("tokens.off"),
    }));
  }
  if (from.announce_all_tools !== to.announce_all_tools) {
    lines.push(t("tokens.diffAnnounceAll", {
      from: from.announce_all_tools ? t("tokens.on") : t("tokens.off"),
      to: to.announce_all_tools ? t("tokens.on") : t("tokens.off"),
    }));
  }
  if (from.confirm_inline_wait_seconds !== to.confirm_inline_wait_seconds) {
    lines.push(t("tokens.diffInlineWait", {
      from: from.confirm_inline_wait_seconds
        ? t("tokens.secondsShort", { n: from.confirm_inline_wait_seconds })
        : t("tokens.off"),
      to: to.confirm_inline_wait_seconds
        ? t("tokens.secondsShort", { n: to.confirm_inline_wait_seconds })
        : t("tokens.off"),
    }));
  }
  if (from.rate_limit_requests !== to.rate_limit_requests || from.rate_limit_burst !== to.rate_limit_burst) {
    lines.push(t("tokens.diffRateLimit", {
      fromRequests: from.rate_limit_requests,
      fromBurst: from.rate_limit_burst,
      toRequests: to.rate_limit_requests,
      toBurst: to.rate_limit_burst,
    }));
  }
  let permDiff = 0;
  for (const level of ["domains", "devices", "entities"] as const) {
    const keys = new Set([...Object.keys(from.permissions[level]), ...Object.keys(to.permissions[level])]);
    for (const k of keys) {
      if (nodeSig(from.permissions[level][k]) !== nodeSig(to.permissions[level][k])) permDiff++;
    }
  }
  if (permDiff > 0) lines.push(t("tokens.diffPermissionTree", { count: permDiff }));
  return lines;
}

// Inline-editable preset name: same onblur-save pattern as EditableTokenName.
function EditablePresetName(
  { tokenId, preset, onUpdate }:
  { tokenId: string; preset: import("../types").TokenPreset; onUpdate: (t: TokenRecord) => void },
) {
  const [value, setValue] = useState(preset.name);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { setValue(preset.name); setErr(null); }, [preset.name]);

  async function commit() {
    const next = value.trim();
    if (next === preset.name) { setValue(preset.name); setErr(null); return; }
    if (!next || next.length > 40) {
      setErr(t("tokens.presetNameError"));
      setValue(preset.name);
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      onUpdate(await api.renamePreset(tokenId, preset.id, next));
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : t("tokens.renameFailed"));
      setValue(preset.name);
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="preset-name-edit">
      <input
        className="preset-name-input"
        value={value}
        disabled={saving}
        spellCheck={false}
        maxLength={40}
        aria-label={t("tokens.presetNameAria", { name: preset.name })}
        title={t("tokens.presetRenameTitle")}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); (e.target as HTMLInputElement).blur(); }
          else if (e.key === "Escape") { setValue(preset.name); setErr(null); (e.target as HTMLInputElement).blur(); }
        }}
      />
      {err && <span className="token-name-error" role="alert">{err}</span>}
    </span>
  );
}

type PresetAction = { kind: "switch" | "revert" | "delete"; preset: import("../types").TokenPreset };

function PresetsCard({ token, onUpdate }: { token: TokenRecord; onUpdate: (t: TokenRecord) => void }) {
  const [action, setAction] = useState<PresetAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [ptConfirmed, setPtConfirmed] = useState(false);

  const active = token.presets.find((p) => p.id === token.active_preset_id) ?? null;
  const liveSnap = snapOfToken(token);
  // Unsaved changes on the active preset: what a switch would save back into it,
  // and what a revert would discard.
  const dirtyLines = active ? snapDiffLines(snapOfPreset(active), liveSnap) : [];
  const dirty = dirtyLines.length > 0;

  async function runAction() {
    if (!action) return;
    setBusy(true);
    setError(null);
    try {
      if (action.kind === "delete") {
        onUpdate(await api.deletePreset(token.id, action.preset.id));
      } else {
        const needsPtConfirm = action.preset.pass_through && !token.pass_through;
        onUpdate(await api.applyPreset(token.id, action.preset.id, needsPtConfirm));
      }
      setAction(null);
      setPtConfirmed(false);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.presetActionFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function createPreset() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      onUpdate(await api.createPreset(token.id, name));
      setNewName("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.presetCreateFailed"));
    } finally {
      setBusy(false);
    }
  }

  const needsPtConfirm = action != null && action.kind !== "delete"
    && action.preset.pass_through && !token.pass_through;
  const applyLines = action && action.kind !== "delete"
    ? snapDiffLines(liveSnap, snapOfPreset(action.preset))
    : [];

  return (
    <div className="preset-list">
      {action && (
        <Modal titleId="preset-action-title" onClose={busy ? undefined : () => { setAction(null); setPtConfirmed(false); setError(null); }}>
          <h3 className="modal-title" id="preset-action-title">
            {action.kind === "switch" && t("tokens.presetSwitchTitle", { name: action.preset.name })}
            {action.kind === "revert" && t("tokens.presetRevertTitle", { name: action.preset.name })}
            {action.kind === "delete" && t("tokens.presetDeleteTitle", { name: action.preset.name })}
          </h3>
          {action.kind === "switch" && (
            <>
              {dirty && active && (
                <div className="preset-modal-section">
                  <p>{tRich("tokens.presetSwitchSaveInto", { strong: (c) => <strong>{c}</strong> }, { name: active.name })}</p>
                  <ul className="preset-diff-list">{dirtyLines.map((l) => <li key={l}>{l}</li>)}</ul>
                </div>
              )}
              <div className="preset-modal-section">
                {applyLines.length > 0
                  ? (
                    <>
                      <p>{tRich("tokens.presetApplying", { strong: (c) => <strong>{c}</strong> }, { name: action.preset.name })}</p>
                      <ul className="preset-diff-list">{applyLines.map((l) => <li key={l}>{l}</li>)}</ul>
                    </>
                  )
                  : <p>{t("tokens.presetNoChange")}</p>}
              </div>
            </>
          )}
          {action.kind === "revert" && (
            <div className="preset-modal-section">
              <p>{tRich("tokens.presetRevertBody", { strong: (c) => <strong>{c}</strong> }, { name: action.preset.name })}</p>
              <ul className="preset-diff-list">{dirtyLines.map((l) => <li key={l}>{l}</li>)}</ul>
            </div>
          )}
          {action.kind === "delete" && (
            <p>{tRich("tokens.presetDeleteBody", { strong: (c) => <strong>{c}</strong> }, { name: action.preset.name })}</p>
          )}
          {needsPtConfirm && (
            <>
              <div className="amber-block">
                <p>{tRich("tokens.presetPtWarning", { strong: (c) => <strong>{c}</strong> })}</p>
              </div>
              <label className="modal-check">
                <input type="checkbox" checked={ptConfirmed} onChange={(e) => setPtConfirmed(e.target.checked)} />
                {t("tokens.presetPtUnderstand")}
              </label>
            </>
          )}
          {error && <ErrorMsg msg={error} />}
          <div className="modal-actions">
            <button className="btn btn-text" onClick={() => { setAction(null); setPtConfirmed(false); setError(null); }} disabled={busy}>{t("tokens.cancel")}</button>
            <button
              className={`btn ${action.kind === "delete" ? "btn-danger" : action.kind === "revert" ? "btn-warning" : "btn-primary"}`}
              onClick={runAction}
              disabled={busy || (needsPtConfirm && !ptConfirmed)}
            >
              {busy ? t("tokens.working") : action.kind === "switch" ? t("tokens.presetSwitch") : action.kind === "revert" ? t("tokens.presetRevert") : t("tokens.delete")}
            </button>
          </div>
        </Modal>
      )}

      {token.presets.map((p) => {
        const isActive = p.id === token.active_preset_id;
        return (
          <div key={p.id} className={`preset-row${isActive ? " preset-row-active" : ""}`}>
            <EditablePresetName tokenId={token.id} preset={p} onUpdate={onUpdate} />
            {isActive && <span className="badge badge-green">{t("tokens.presetActive")}</span>}
            {isActive && dirty && <span className="badge badge-amber" title={t("tokens.presetModifiedTitle")}>{t("tokens.presetModified")}</span>}
            <span className="preset-row-actions">
              {!isActive && (
                <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => setAction({ kind: "switch", preset: p })}>
                  {t("tokens.presetSwitchTo")}
                </button>
              )}
              {isActive && dirty && (
                <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => setAction({ kind: "revert", preset: p })}>
                  {t("tokens.presetRevertToSaved")}
                </button>
              )}
              {!isActive && (
                <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setAction({ kind: "delete", preset: p })} aria-label={t("tokens.presetDeleteAria", { name: p.name })}>
                  {t("tokens.delete")}
                </button>
              )}
            </span>
          </div>
        );
      })}
      {token.presets.length === 0 && (
        <p className="preset-empty">{t("tokens.presetEmpty")}</p>
      )}

      <div className="preset-create-row">
        <input
          className="input"
          placeholder={t("tokens.presetNewName")}
          value={newName}
          maxLength={40}
          disabled={busy || token.presets.length >= MAX_PRESETS}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") createPreset(); }}
          aria-label={t("tokens.presetNewName")}
        />
        <button
          className="btn btn-outline btn-sm"
          disabled={busy || !newName.trim() || token.presets.length >= MAX_PRESETS}
          onClick={createPreset}
        >
          {t("tokens.presetSaveCurrent")}
        </button>
      </div>
      {token.presets.length >= MAX_PRESETS && (
        <small className="preset-hint">{t("tokens.presetMax", { max: MAX_PRESETS })}</small>
      )}
      {!action && error && <ErrorMsg msg={error} />}
      <small className="preset-hint">
        {t("tokens.presetHint")}
      </small>
    </div>
  );
}

// Inline-editable token name shown as the detail heading. Click to select/edit;
// it auto-saves on blur (and on Enter), validating format client-side and letting
// the server reject a name that clashes with another token. Escape cancels.
const TOKEN_NAME_RE = /^[A-Za-z0-9_-]{3,32}$/;

function EditableTokenName({ token, onRenamed }: { token: TokenRecord; onRenamed: (t: TokenRecord) => void }) {
  const [value, setValue] = useState(token.name);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setValue(token.name); setErr(null); }, [token.name]);

  async function commit() {
    const next = value.trim();
    if (next === token.name) { setValue(token.name); setErr(null); return; }
    if (!TOKEN_NAME_RE.test(next)) {
      setErr(t("tokens.nameRuleError"));
      setValue(token.name);
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const updated = await api.patchToken(token.id, { name: next });
      onRenamed(updated);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : t("tokens.renameFailed"));
      setValue(token.name);
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="token-name-edit">
      <input
        className="token-card-name token-name-input"
        value={value}
        disabled={saving}
        spellCheck={false}
        maxLength={32}
        aria-label={t("tokens.tokenNameAria")}
        title={t("tokens.tokenRenameTitle")}
        onChange={(e) => setValue(e.target.value)}
        onFocus={(e) => e.target.select()}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); (e.target as HTMLInputElement).blur(); }
          else if (e.key === "Escape") { setValue(token.name); setErr(null); (e.target as HTMLInputElement).blur(); }
        }}
      />
      {err && <span className="token-name-error" role="alert">{err}</span>}
    </span>
  );
}

export function TokenDetailView({ tokenId, onBack, onRefresh, presetsEnabled = false, esphome = null }: Props) {
  const [token, setToken] = useState<TokenRecord | null>(null);
  const [mesaProfileEntities, setMesaProfileEntities] = useState<Set<string>>(new Set());
  const [mesaProfileDevices, setMesaProfileDevices] = useState<Set<string>>(new Set());
  const [mesaProfileDomains, setMesaProfileDomains] = useState<Set<string>>(new Set());
  // The entity whose MESA profile is being edited inline (overlaid on this tab),
  // and the canonical tag vocabulary the editor needs.
  const [mesaEdit, setMesaEdit] = useState<{ scope: LinkScope; key: string; label?: string; isNew: boolean } | null>(null);
  const [canonicalTags, setCanonicalTags] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // One destructive action at a time, by construction. This was four
  // independent show/busy pairs (revoke, rotate, pass-through, clear
  // permissions): eight booleans encoding one question, spreading each action's
  // state across two variables and leaving "two modals open at once"
  // representable even though it is never wanted.
  const [action, setAction] = useState<{ kind: TokenAction; busy: boolean } | null>(null);
  const openAction = (kind: TokenAction) => setAction({ kind, busy: false });
  const closeAction = () => setAction(null);
  const setActionBusy = (busy: boolean) => setAction((a) => (a ? { ...a, busy } : a));
  const isOpen = (kind: TokenAction) => action?.kind === kind;
  const isBusy = (kind: TokenAction) => action?.kind === kind && action.busy;
  const [rotatedRawToken, setRotatedRawToken] = useState<string | null>(null);
  const [showSelectByPicker, setShowSelectByPicker] = useState(false);
  const [entityTree, setEntityTree] = useState<import("../types").EntityTree | null>(null);
  // Set when a change alters the announced tool list, so we can remind the operator
  // to reconnect the agent. Only surfaced when the token has actually been used.
  const [reconnectNeeded, setReconnectNeeded] = useState(false);
  const [selectedEntityId, setSelectedEntityId] = useState("");
  const [selectedDepth, setSelectedDepth] = useState<"entity" | "device" | "domain">("entity");
  // Bumped on every reveal request so clicking the same row re-triggers the
  // tree expand/scroll (selecting the same id/depth alone is a no-op in React).
  const [revealNonce, setRevealNonce] = useState(0);
  // opts.reveal defaults true (click-to-jump from elsewhere on the page, e.g.
  // the Permission Summary table, still scrolls/expands/flashes the target).
  // Editing a permission directly in the tree passes reveal: false: the row
  // is already on screen, so scrolling it to center only risks the page
  // moving mid-click and the next click landing on a different row.
  const revealNode = (eid: string, depth: "entity" | "device" | "domain" = "entity", opts?: { reveal?: boolean }) => {
    setSelectedEntityId(eid);
    setSelectedDepth(depth);
    if (opts?.reveal !== false) {
      setRevealNonce((n) => n + 1);
    }
  };
  const [permissionsVersion, setPermissionsVersion] = useState(0);
  const [collapseTreeKey, setCollapseTreeKey] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getToken(tokenId);
      setToken(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [tokenId]);

  useEffect(() => { load(); }, [load]);

  // setToken wrapper for the config editors (persona, capabilities, tool
  // announcement, pass-through). If the change alters the announced tool list and
  // the token has been used by a client, raise the reconnect reminder.
  const applyTokenUpdate = useCallback((updated: TokenRecord) => {
    if (token && updated.last_used_at && toolGatingChanged(token, updated)) {
      setReconnectNeeded(true);
    }
    setToken(updated);
  }, [token]);

  useEffect(() => {
    api.getEntityTree().then(setEntityTree).catch(() => null);
  }, []);

  // Which entities already have a MESA profile, so the cards can show
  // "view" (MESA) vs "create" (+). Reloaded after the inline editor saves so a
  // newly created profile flips its "+" to "MESA" without leaving the tab.
  const loadMesaProfiles = useCallback(() => {
    api.listMesaProfiles({ limit: 500 })
      .then((r) => setMesaProfileEntities(new Set(r.profiles.map((p) => p.entity_id))))
      .catch(() => null);
    // The tree offers device and domain profiles too, so it needs to know which
    // of those already exist to show "MESA" rather than "+". Separate reads
    // because each scope is its own endpoint; a failure just leaves that scope
    // showing "+", which opens the editor and loads the real profile anyway.
    api.listMesaDevices()
      .then((r) => setMesaProfileDevices(new Set(r.devices.map((d) => d.device_id))))
      .catch(() => null);
    api.listMesaDomains()
      .then((r) => setMesaProfileDomains(new Set(r.domains.map((d) => d.domain))))
      .catch(() => null);
  }, []);
  useEffect(() => { loadMesaProfiles(); }, [loadMesaProfiles]);

  // The canonical MESA tag vocabulary powers the inline editor's tag autocomplete.
  useEffect(() => { api.getMesaVocabulary().then((v) => setCanonicalTags(v.canonical_tags)).catch(() => null); }, []);

  // Open the MESA profile editor as an overlay on this tab (no tab switch). isNew
  // mirrors the +/MESA affordance the user clicked (driven by the same set).
  const openMesaInline = useCallback((key: string, scope: LinkScope = "entity", label?: string) => {
    // isNew mirrors the +/MESA affordance that was clicked, which is driven by
    // the same set, so the editor opens in the mode the operator just saw.
    const known = scope === "device" ? mesaProfileDevices
      : scope === "domain" ? mesaProfileDomains
      : mesaProfileEntities;
    setMesaEdit({ scope, key, label, isNew: !known.has(key) });
  }, [mesaProfileEntities, mesaProfileDevices, mesaProfileDomains]);

  async function revoke() {
    setActionBusy(true);
    try {
      await api.revokeToken(tokenId);
      // Any open Agent Chat window (panel or floating) drops the revoked token
      // from its own list and reselects/clears, instead of continuing to show
      // (and let the user submit against) a token that no longer works.
      window.dispatchEvent(new CustomEvent("phx-tokens-changed"));
      onBack();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.revokeFailed"));
      closeAction();
    }
  }

  async function rotate() {
    setActionBusy(true);
    try {
      const resp = await api.rotateToken(tokenId);
      const { token: rawToken } = resp as { token: string };
      setRotatedRawToken(rawToken);
      onRefresh?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.rotateFailed"));
    } finally {
      // Closed on success and on failure alike, as before.
      closeAction();
    }
  }

  async function clearPermissions() {
    setActionBusy(true);
    try {
      const updatedTree = await api.setPermissions(tokenId, { domains: {}, devices: {}, entities: {} });
      setToken((prev) => prev ? { ...prev, permissions: updatedTree } : prev);
      setPermissionsVersion((v) => v + 1);
      setCollapseTreeKey((k) => k + 1);
      closeAction();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.clearPermsFailed"));
      // Unlike the others, this modal stays open on failure so the operator can
      // retry without reopening it.
      setActionBusy(false);
    }
  }

  async function enablePassThrough() {
    setActionBusy(true);
    try {
      const body: PatchTokenBody = { pass_through: true, confirm_pass_through: true };
      const updated = await api.patchToken(tokenId, body);
      applyTokenUpdate(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.enablePtFailed"));
    } finally {
      closeAction();
    }
  }

  if (loading) return <Loading />;
  if (error && !token) return <div><button className="btn btn-text" onClick={onBack}>{t("tokens.back")}</button><ErrorMsg msg={error} /></div>;
  if (!token) return null;

  if (rotatedRawToken) {
    return <RotatedTokenModal rawToken={rotatedRawToken} tokenName={token.name} onClose={() => setRotatedRawToken(null)} />;
  }

  const status = tokenStatus(token);
  const statusClass = status === "Active" ? "badge-green" : status === "Expired" ? "badge-grey" : "badge-red";

  // One-line summaries shown on the collapsed Advanced cards.
  const personaDef = PERSONAS.find((p) => p.key === token.persona);
  const personaLabel = personaDef ? t(personaDef.labelKey) : t("personas.custom.label");
  const capCounts = CAP_NAMES.reduce(
    (acc, k) => { acc[token[k]] = (acc[token[k]] ?? 0) + 1; return acc; },
    { allow: 0, confirm: 0, deny: 0 } as Record<string, number>,
  );
  const capsSummary = t("tokens.capsSummary", {
    persona: personaLabel,
    allow: capCounts.allow,
    confirm: capCounts.confirm,
    deny: capCounts.deny,
  });
  const announceSummary = token.announce_all_tools
    ? t("tokens.announceSummaryAll")
    : t("tokens.announceSummaryScoped");
  const rateSummary = token.rate_limit_requests > 0
    ? t("tokens.rateSummary", { requests: token.rate_limit_requests, burst: token.rate_limit_burst })
    : t("tokens.rateSummaryNone");
  const activePreset = token.presets.find((p) => p.id === token.active_preset_id) ?? null;
  const presetsSummary = activePreset
    ? (snapDiffLines(snapOfPreset(activePreset), snapOfToken(token)).length > 0
      ? t("tokens.presetsSummaryModified", { name: activePreset.name })
      : activePreset.name)
    : t("tokens.presetsSummarySaved", { count: token.presets.length });

  return (
    <div className="token-detail-wrap">

      {/* Modals */}
      {isOpen("rotate") && (
        <ConfirmModal
          title={t("tokens.rotateTokenTitle")}
          body={
            <div className="amber-block">
              <p>{t("tokens.rotateBody")}</p>
            </div>
          }
          checkLabel={t("tokens.rotateCheck")}
          confirmLabel={t("tokens.rotateTokenTitle")}
          confirmClass="btn-primary"
          loading={isBusy("rotate")}
          onConfirm={rotate}
          onClose={closeAction}
        />
      )}

      {isOpen("revoke") && (
        <ConfirmModal
          title={t("tokens.revokeTokenTitle")}
          body={
            <div className="amber-block">
              <p>{t("tokens.revokeBody")}</p>
            </div>
          }
          checkLabel={t("tokens.revokeCheck")}
          confirmLabel={t("tokens.revokeTokenTitle")}
          confirmClass="btn-danger"
          loading={isBusy("revoke")}
          onConfirm={revoke}
          onClose={closeAction}
        />
      )}

      {isOpen("passThrough") && (
        <ConfirmModal
          title={t("tokens.enablePtTitle")}
          body={
            <div className="amber-block">
              <p>{tRich("tokens.enablePtBody", { strong: (c) => <strong>{c}</strong> })}</p>
            </div>
          }
          checkLabel={t("tokens.enablePtCheck")}
          confirmLabel={t("tokens.enablePtLabel")}
          confirmClass="btn-warning"
          loading={isBusy("passThrough")}
          onConfirm={enablePassThrough}
          onClose={closeAction}
        />
      )}

      {/* Sticky top section */}
      <div className="token-detail-sticky">
        {error && <ErrorMsg msg={error} />}

        {reconnectNeeded && (
          <div className="banner banner-info reconnect-banner" role="status">
            <span className="reconnect-banner-text">
              {tRich("tokens.reconnectBanner", { strong: (c) => <strong>{c}</strong> })}
            </span>
            <button
              className="reconnect-banner-dismiss"
              onClick={() => setReconnectNeeded(false)}
              aria-label={t("tokens.reconnectDismiss")}
            >&times;</button>
          </div>
        )}

        {token.pass_through && (
          <div className="pass-through-header-banner">
            <p>
              {tRich("tokens.ptHeaderBanner", { strong: (c) => <strong className="text-warning">{c}</strong> })}
            </p>
          </div>
        )}

        <div className="two-col">
          {/* Left: Token info card */}
          <div className="card token-info-card">
            <div className="token-card-header">
              <div className="token-card-name-row">
                <img src={PHOENIX_ICON} className="token-card-icon" alt="" />
                <EditableTokenName token={token} onRenamed={(u) => { applyTokenUpdate(u); onRefresh?.(); }} />
              </div>
              <div className="token-card-badges">
                <span className={`badge ${statusClass}`}>{tokenStatusLabel(status)}</span>
                {token.pass_through
                  ? <span className="badge badge-amber">{t("tokens.passThrough")}</span>
                  : <span className="badge badge-blue">{t("tokens.scoped")}</span>}
              </div>
            </div>

            <div className="token-card-body">
              <div className="token-card-meta">
                <div className="token-meta-table">
                  <span className="stat-label">{t("tokens.colCreated")}</span>
                  <span title={token.created_at ? localeDateTime(token.created_at) : undefined}>{formatDateTime(token.created_at)}</span>
                  <span className="stat-label">{t("tokens.colUpdated")}</span>
                  <span title={token.updated_at ? localeDateTime(token.updated_at) : undefined}>{formatDateTime(token.updated_at)}</span>
                  <span className="stat-label">{t("tokens.colExpires")}</span>
                  <span>{formatDateTime(token.expires_at)}</span>
                  <span className="stat-label">{t("tokens.colLastUsed")}</span>
                  <span>{formatDateTime(token.last_used_at)}</span>
                </div>
              </div>

              <div className="token-card-actions">
                <button className="btn btn-outline btn-sm token-action-btn" onClick={() => openAction("rotate")}>
                  {t("tokens.rotate")}
                </button>
                {!token.pass_through && (
                  <button className="btn btn-warning btn-sm token-action-btn" onClick={() => openAction("passThrough")}>
                    {t("tokens.enablePtLabel")}
                  </button>
                )}
                <button className="btn btn-danger btn-sm token-action-btn" onClick={() => openAction("revoke")}>
                  {t("tokens.revoke")}
                </button>
              </div>
            </div>
          </div>

          {/* Right: Permission emulator */}
          <div className="card epe-card">
            <h3 className="card-header">
              {t("tokens.emulatorTitle")}
              <DocsHelpLink path="panel.html#emulator" label={t("tokens.emulatorTitle")} />
            </h3>
            {token.pass_through ? (
              <p style={{ fontSize: 13, color: "var(--phx-text-2)", margin: 0 }}>
                {t("tokens.emulatorPassThrough")}
              </p>
            ) : (
              <PermissionSimulator
                tokenId={tokenId}
                externalEntityId={selectedEntityId || undefined}
                resolveDepth={selectedDepth}
                triggerVersion={permissionsVersion}
                mesaProfileEntities={mesaProfileEntities}
                onOpenMesa={openMesaInline}
              />
            )}
          </div>
        </div>
      </div>

      {/* Scrollable body */}
      <div className="token-detail-body">
        <div className="two-col">
          <div>
            <CollapsibleCard title={t("tokens.cardPersona")} summary={personaLabel} defaultOpen persistKey="phx:fold:persona" docsPath="capabilities.html#personas">
              <PersonaPicker token={token} onUpdate={applyTokenUpdate} esphome={esphome} />
            </CollapsibleCard>

            {presetsEnabled && (
              <CollapsibleCard title={t("tokens.cardPresets")} summary={presetsSummary} persistKey="phx:fold:presets" docsPath="panel.html#presets">
                <PresetsCard token={token} onUpdate={applyTokenUpdate} />
              </CollapsibleCard>
            )}

            <div className="advanced-section-label">{t("tokens.advanced")}</div>
            <CollapsibleCard title={t("tokens.cardCapabilities")} summary={capsSummary} persistKey="phx:fold:capabilities" docsPath="capabilities.html#capability-flags">
              <CapabilityMatrix token={token} onUpdate={applyTokenUpdate} esphome={esphome} />
            </CollapsibleCard>
            <CollapsibleCard title={t("tokens.cardToolAnnouncement")} summary={announceSummary} persistKey="phx:fold:announce" docsPath="operations.html#advanced-token-options">
              <ToolAnnouncementToggle token={token} onUpdate={applyTokenUpdate} />
            </CollapsibleCard>
            <CollapsibleCard title={t("tokens.cardRateLimiting")} summary={rateSummary} persistKey="phx:fold:ratelimit" docsPath="operations.html#rate-limiting">
              <RateLimitConfig token={token} onUpdate={setToken} />
            </CollapsibleCard>

            {!token.pass_through && (
              <div className="card">
                <h3 className="card-header">
                  {t("tokens.permissionSummary")}
                  <DocsHelpLink path="panel.html#summary" label={t("tokens.permissionSummary")} />
                </h3>
                <PermissionSummary
                  permissions={token.permissions}
                  entityTree={entityTree}
                  onEntityClick={revealNode}
                  mesaProfileEntities={mesaProfileEntities}
                  onOpenMesa={openMesaInline}
                />
              </div>
            )}
          </div>

          <div>
            {token.pass_through ? (
              <div className="card">
                <h3 className="card-header">
                  {t("tokens.permissionsTree")}
                  <DocsHelpLink path="panel.html#permission-tree" label={t("tokens.permissionsTree")} />
                </h3>
                <PassThroughNotice token={token} onUpdate={applyTokenUpdate} />
              </div>
            ) : (
              <div className="card">
                <div className="card-header card-header-stack">
                  <h3 className="card-header-title">
                    {t("tokens.permissionsTree")}
                    <DocsHelpLink path="panel.html#permission-tree" label={t("tokens.permissionsTree")} />
                  </h3>
                  <div className="tree-header-actions">
                    {entityTree && (
                      <button className="btn btn-outline btn-sm" onClick={() => setShowSelectByPicker(true)}>
                        {t("tokens.selectByAreaLabelIntegration")}
                      </button>
                    )}
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => openAction("clearPerms")}
                    >
                      {t("tokens.clearAll")}
                    </button>
                  </div>
                </div>
                <EntityTree
                  tokenId={tokenId}
                  permissions={token.permissions}
                  onPermissionsChange={(tree) => {
                    setToken({ ...token, permissions: tree });
                    setPermissionsVersion((v) => v + 1);
                  }}
                  onEntityClick={revealNode}
                  collapseKey={collapseTreeKey}
                  revealEntity={selectedEntityId || undefined}
                  revealDepth={selectedDepth}
                  revealNonce={revealNonce}
                  mesaProfileEntities={mesaProfileEntities}
                  mesaProfileDevices={mesaProfileDevices}
                  mesaProfileDomains={mesaProfileDomains}
                  onOpenMesa={openMesaInline}
                />
              </div>
            )}
          </div>
        </div>
      </div>

      {showSelectByPicker && entityTree && (
        <SelectByPicker
          tokenId={tokenId}
          entityTree={entityTree}
          onDone={() => {
            setShowSelectByPicker(false);
            load();
          }}
          onClose={() => setShowSelectByPicker(false)}
        />
      )}

      {isOpen("clearPerms") && (
        <Modal titleId="clear-perms-title" onClose={isBusy("clearPerms") ? undefined : closeAction}>
          <h3 className="modal-title" id="clear-perms-title">{t("tokens.clearPermsTitle")}</h3>
          <p className="clear-perms-body">
            {t("tokens.clearPermsBody")}
          </p>
          <div className="modal-actions">
            <button className="btn btn-danger" onClick={clearPermissions} disabled={isBusy("clearPerms")}>
              {isBusy("clearPerms") ? t("tokens.clearing") : t("tokens.clearAll")}
            </button>
            <button className="btn btn-text" onClick={closeAction} disabled={isBusy("clearPerms")}>
              {t("tokens.cancel")}
            </button>
          </div>
        </Modal>
      )}

      {mesaEdit && (
        <ProfileEditor
          scope={mesaEdit.scope}
          profileKey={mesaEdit.key}
          keyLabel={mesaEdit.label}
          isNew={mesaEdit.isNew}
          entityTree={entityTree}
          canonicalTags={canonicalTags}
          onClose={() => setMesaEdit(null)}
          onSaved={loadMesaProfiles}
          // The target is whichever row was clicked, so it is fixed rather than
          // picked. Without this the editor shows its combobox and validates the
          // pre-filled key against a picker source this view does not supply,
          // which rejected every device id as "no matching device". Locking is
          // also what the in-context injector does, and for the same reason: the
          // caller already knows the target, so re-deriving it can only go wrong.
          lockedKey
        />
      )}
    </div>
  );
}
