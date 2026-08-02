import React from "react";
import type { Persona, TokenRecord } from "../types";
import { api } from "../api";
import { PERSONAS, PERSONA_CAP_DEFAULTS } from "../personas";
import type { EsphomeAvailability } from "./CapabilityMatrix";
import { t } from "../i18n";

// Patch mode: applies the persona to an existing token (Token Detail).
// Select mode: no token yet (onboarding wizard); reports the chosen persona to
// the parent, which applies it after the token is created.
interface PatchProps {
  token: TokenRecord;
  onUpdate: (updated: TokenRecord) => void;
  selected?: undefined;
  onSelect?: undefined;
  // Only the Token Detail view has settings in hand. The wizard leaves this
  // undefined, so no note renders there.
  esphome?: EsphomeAvailability | null;
}
interface SelectProps {
  token?: undefined;
  onUpdate?: undefined;
  selected: Persona | null;
  onSelect: (persona: Persona) => void;
}
type Props = PatchProps | SelectProps;

export function PersonaPicker(props: Props) {
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const isSelectMode = props.onSelect !== undefined;
  const esphome = isSelectMode ? null : props.esphome;
  const activeKey = isSelectMode ? props.selected : props.token.persona;

  async function choose(persona: Persona) {
    if (isSelectMode) {
      props.onSelect(persona);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // Fetch the persona's cap defaults from a hardcoded mirror of personas.py.
      // Backend validation is the source of truth; this is a UX shortcut so we
      // can patch all caps in one request.
      const caps = PERSONA_CAP_DEFAULTS[persona];
      const body: Record<string, unknown> = { persona };
      if (caps) {
        Object.assign(body, caps);
      }
      const updated = await api.patchToken(props.token.id, body);
      props.onUpdate(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("tokens.personaApplyFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="persona-picker">
      {error && <div className="banner banner-error mb-8">{error}</div>}
      {!isSelectMode && (
        <p className="persona-helper">{t("personas.helper")}</p>
      )}
      <div className="persona-grid">
        {PERSONAS.filter((p) =>
          isSelectMode
            ? p.key !== "custom"   // wizard: hide "Custom" (no matrix to configure)
            : !p.wizardOnly,       // token detail: hide wizard-only presets
        ).map((p) => {
          const active = activeKey === p.key;
          return (
            <button
              key={p.key}
              type="button"
              className={`persona-card${active ? " persona-card-active" : ""}`}
              onClick={() => !saving && !active && choose(p.key)}
              disabled={saving}
              aria-pressed={active}
            >
              <div className="persona-card-label">{t(p.labelKey)}</div>
              <div className="persona-card-desc">{t(p.descriptionKey)}</div>
              {p.key === "esphome" && esphome && !esphome.integration && !esphome.builder && (
                <div className="persona-card-note">{t("personas.noEsphome")}</div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
