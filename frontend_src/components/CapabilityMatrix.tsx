import React from "react";
import type { CapMode, CapName, CapTier, TokenRecord, PatchTokenBody } from "../types";
import { api } from "../api";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";

export interface EsphomeAvailability {
  integration: boolean;
  builder: boolean;
}

interface Props {
  token: TokenRecord;
  onUpdate: (updated: TokenRecord) => void;
  // What ESPHome surfaces this system has, when known. The row stays settable
  // either way so a token can be prepared before ESPHome is installed; this only
  // explains why the tools are not showing up yet.
  esphome?: EsphomeAvailability | null;
}

function esphomeNotice(esphome: EsphomeAvailability | null | undefined): string | null {
  if (!esphome || esphome.builder) return null;
  return esphome.integration
    ? t("caps.noBuilder")
    : t("caps.noEsphome");
}

interface CapDef {
  key: CapName;
  labelKey: string;
  descriptionKey: string;
  tier: CapTier;
  confirmAvailable: boolean;
}

const CAPS: CapDef[] = [
  {
    key: "cap_config_read",
    labelKey: "caps.cap_config_read.label",
    descriptionKey: "caps.cap_config_read.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_camera_read",
    labelKey: "caps.cap_camera_read.label",
    descriptionKey: "caps.cap_camera_read.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_template_render",
    labelKey: "caps.cap_template_render.label",
    descriptionKey: "caps.cap_template_render.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_log_read",
    labelKey: "caps.cap_log_read.label",
    descriptionKey: "caps.cap_log_read.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_search",
    labelKey: "caps.cap_search.label",
    descriptionKey: "caps.cap_search.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_registry_read",
    labelKey: "caps.cap_registry_read.label",
    descriptionKey: "caps.cap_registry_read.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_traces",
    labelKey: "caps.cap_traces.label",
    descriptionKey: "caps.cap_traces.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_diagnostics",
    labelKey: "caps.cap_diagnostics.label",
    descriptionKey: "caps.cap_diagnostics.description",
    tier: "read",
    confirmAvailable: false,
  },
  {
    key: "cap_broadcast",
    labelKey: "caps.cap_broadcast.label",
    descriptionKey: "caps.cap_broadcast.description",
    tier: "everyday",
    confirmAvailable: false,
  },
  {
    key: "cap_service_response",
    labelKey: "caps.cap_service_response.label",
    descriptionKey: "caps.cap_service_response.description",
    tier: "everyday",
    confirmAvailable: false,
  },
  {
    key: "cap_automation_write",
    labelKey: "caps.cap_automation_write.label",
    descriptionKey: "caps.cap_automation_write.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_script_write",
    labelKey: "caps.cap_script_write.label",
    descriptionKey: "caps.cap_script_write.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_blueprint_write",
    labelKey: "caps.cap_blueprint_write.label",
    descriptionKey: "caps.cap_blueprint_write.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_scene_write",
    labelKey: "caps.cap_scene_write.label",
    descriptionKey: "caps.cap_scene_write.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_helper_write",
    labelKey: "caps.cap_helper_write.label",
    descriptionKey: "caps.cap_helper_write.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_physical_control",
    labelKey: "caps.cap_physical_control.label",
    descriptionKey: "caps.cap_physical_control.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_restart",
    labelKey: "caps.cap_restart.label",
    descriptionKey: "caps.cap_restart.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_log_control",
    labelKey: "caps.cap_log_control.label",
    descriptionKey: "caps.cap_log_control.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_integration_write",
    labelKey: "caps.cap_integration_write.label",
    descriptionKey: "caps.cap_integration_write.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_integration_reconfigure",
    labelKey: "caps.cap_integration_reconfigure.label",
    descriptionKey: "caps.cap_integration_reconfigure.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_lovelace_write",
    labelKey: "caps.cap_lovelace_write.label",
    descriptionKey: "caps.cap_lovelace_write.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_registry_write",
    labelKey: "caps.cap_registry_write.label",
    descriptionKey: "caps.cap_registry_write.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_radio_write",
    labelKey: "caps.cap_radio_write.label",
    descriptionKey: "caps.cap_radio_write.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_energy_write",
    labelKey: "caps.cap_energy_write.label",
    descriptionKey: "caps.cap_energy_write.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_backup",
    labelKey: "caps.cap_backup.label",
    descriptionKey: "caps.cap_backup.description",
    tier: "irreversible",
    confirmAvailable: true,
  },
  {
    key: "cap_filesystem",
    labelKey: "caps.cap_filesystem.label",
    descriptionKey: "caps.cap_filesystem.description",
    tier: "irreversible",
    confirmAvailable: true,
  },
  {
    key: "cap_esphome_yaml",
    labelKey: "caps.cap_esphome_yaml.label",
    descriptionKey: "caps.cap_esphome_yaml.description",
    tier: "config_write",
    confirmAvailable: true,
  },
  {
    key: "cap_esphome_flash",
    labelKey: "caps.cap_esphome_flash.label",
    descriptionKey: "caps.cap_esphome_flash.description",
    tier: "system",
    confirmAvailable: true,
  },
  {
    key: "cap_yaml_edit",
    labelKey: "caps.cap_yaml_edit.label",
    descriptionKey: "caps.cap_yaml_edit.description",
    tier: "irreversible",
    confirmAvailable: true,
  },
];

// All capability keys, in matrix order. Exported so other views (e.g. the
// Token Detail summary) can tally allow/confirm/deny without re-listing caps.
export const CAP_NAMES: CapName[] = CAPS.map((c) => c.key);

const TIER_LABEL_KEYS: Record<CapTier, string> = {
  read: "caps.tierRead",
  everyday: "caps.tierEveryday",
  config_write: "caps.tierConfigWrite",
  system: "caps.tierSystem",
  irreversible: "caps.tierIrreversible",
};

const TIER_ORDER: CapTier[] = ["read", "everyday", "config_write", "system", "irreversible"];

const MODE_LABEL_KEYS: Record<CapMode, string> = {
  deny: "caps.modeDeny",
  allow: "caps.modeAllow",
  confirm: "caps.modeConfirm",
};

const MODE_DESC_KEYS: Record<CapMode, string> = {
  deny: "caps.modeDescDeny",
  allow: "caps.modeDescAllow",
  confirm: "caps.modeDescConfirm",
};

export function CapabilityMatrix({ token, onUpdate, esphome }: Props) {
  const [saving, setSaving] = React.useState<CapName | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function setMode(cap: CapName, mode: CapMode) {
    if (token[cap] === mode) return;
    setSaving(cap);
    setError(null);
    try {
      const body = { [cap]: mode } as unknown as PatchTokenBody;
      const updated = await api.patchToken(token.id, body);
      onUpdate(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("caps.updateFailed"));
    } finally {
      setSaving(null);
    }
  }

  // Group caps by tier and preserve order.
  const grouped: Record<CapTier, CapDef[]> = {
    read: [],
    everyday: [],
    config_write: [],
    system: [],
    irreversible: [],
  };
  for (const def of CAPS) grouped[def.tier].push(def);

  return (
    <div className="capability-matrix">
      {error && <div className="banner banner-error mb-8">{error}</div>}
      {token.pass_through && (
        <div className="amber-block mb-8">
          <p>
            {tRich("caps.passThroughNotice", { strong: (c) => <strong>{c}</strong> })}
          </p>
        </div>
      )}
      {TIER_ORDER.map((tier) => {
        const items = grouped[tier];
        if (items.length === 0) return null;
        return (
          <div key={tier} className="cap-tier-group">
            <div className="cap-tier-header">{t(TIER_LABEL_KEYS[tier])}</div>
            {items.map((cap) => {
              const current = token[cap.key];
              const isSaving = saving === cap.key;
              const notice = cap.key === "cap_esphome_yaml" || cap.key === "cap_esphome_flash"
                ? esphomeNotice(esphome) : null;
              return (
                <div key={cap.key} className="cap-row">
                  <div className="cap-row-label">
                    <div className="cap-row-name">
                      {t(cap.labelKey)}
                      {notice && (
                        <span
                          className="badge badge-amber cap-row-badge"
                          title={t("caps.esphomeBadgeTitle")}
                        >
                          {notice}
                        </span>
                      )}
                    </div>
                    <div className="cap-row-desc">{t(cap.descriptionKey)}</div>
                  </div>
                  <div className="cap-row-modes" role="radiogroup" aria-label={t(cap.labelKey)}>
                    <ModeRadio
                      cap={cap.key}
                      mode="deny"
                      current={current}
                      onSelect={setMode}
                      disabled={isSaving}
                    />
                    <ModeRadio
                      cap={cap.key}
                      mode="allow"
                      current={current}
                      onSelect={setMode}
                      disabled={isSaving}
                    />
                    <ModeRadio
                      cap={cap.key}
                      mode="confirm"
                      current={current}
                      onSelect={setMode}
                      disabled={isSaving || !cap.confirmAvailable}
                      unavailable={!cap.confirmAvailable}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

interface ModeRadioProps {
  cap: CapName;
  mode: CapMode;
  current: CapMode;
  onSelect: (cap: CapName, mode: CapMode) => void;
  disabled: boolean;
  unavailable?: boolean;
}

function ModeRadio({ cap, mode, current, onSelect, disabled, unavailable }: ModeRadioProps) {
  const checked = current === mode;
  const id = `${cap}-${mode}`;
  return (
    <label
      htmlFor={id}
      className={`mode-radio mode-${mode}${checked ? " mode-radio-checked" : ""}${unavailable ? " mode-radio-unavailable" : ""}`}
      title={unavailable ? t("caps.confirmUnavailable") : t(MODE_DESC_KEYS[mode])}
    >
      <input
        id={id}
        type="radio"
        name={cap}
        value={mode}
        checked={checked}
        disabled={disabled}
        onChange={() => onSelect(cap, mode)}
      />
      <span className="mode-radio-dot" />
      <span className="mode-radio-label">{t(MODE_LABEL_KEYS[mode])}</span>
    </label>
  );
}
