import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type {
  EntityTree as EntityTreeData,
  MesaExportArchive,
  MesaImportResult,
  MesaProfileDetail,
  MesaProfileDocument,
  MesaProfileListItem,
  MesaProfileScope,
  MesaIssuesResponse,
  MesaValidationIssue,
} from "../types";
import { api, ApiError } from "../api";
import { Modal } from "../components/Modal";
import { TagInput } from "../components/TagInput";
import { MesaSuggestions } from "../components/MesaSuggestions";
import { Loading, ErrorMsg, RefreshIcon, ExportIcon, ImportIcon } from "../components/common";
import { compareStrings, t, tn } from "../i18n";
import { tRich } from "../i18n/rich";

// HA domain -> the canonical tag namespace roots that describe an ENTITY of that
// domain, used to surface "Suggested" tags. Intent namespaces (automation, scene)
// describe automations/scenes, not entities, so they're only suggested for those
// domains. Falls back to a general set when unmapped.
const DOMAIN_TAG_ROOTS: Record<string, string[]> = {
  light: ["lighting"],
  switch: ["energy", "resource"],
  climate: ["climate"],
  fan: ["climate"],
  cover: ["security"],
  lock: ["security"],
  alarm_control_panel: ["security"],
  camera: ["security", "diagnostic"],
  binary_sensor: ["presence", "security"],
  sensor: ["energy", "diagnostic", "presence"],
  media_player: ["media", "audio"],
  person: ["person", "presence"],
  device_tracker: ["presence", "person"],
  scene: ["scene"],
  automation: ["automation"],
  script: ["automation"],
  vacuum: ["resource"],
  number: ["helper"],
  select: ["helper"],
  input_boolean: ["helper"],
  input_number: ["helper"],
  input_select: ["helper"],
  input_text: ["helper"],
  input_datetime: ["helper"],
};
const FALLBACK_TAG_ROOTS = ["space", "zone", "diagnostic"];

// Option value plus a human-readable label. The stored value is always the slug
// (what mesa-core expects); the label is only for display.
type Opt = { value: string; label: string };
// A fixed option set instead carries the catalog key for its label.
type LabeledOpt = { value: string; labelKey: string };

const CONTROL_MODES: LabeledOpt[] = [
  { value: "autonomous", labelKey: "mesa.modeAutonomous" },
  { value: "confirm", labelKey: "mesa.modeConfirmOption" },
  { value: "read_only", labelKey: "mesa.modeReadOnly" },
  { value: "prohibited", labelKey: "mesa.modeProhibited" },
];
const TRIGGERS: LabeledOpt[] = [
  { value: "unknown", labelKey: "mesa.trigUnknown" },
  { value: "none", labelKey: "mesa.trigNone" },
  { value: "likely", labelKey: "mesa.trigLikely" },
  { value: "deployment_defined", labelKey: "mesa.trigDeploymentDefined" },
];
const REVERSIBILITY_COSTS: LabeledOpt[] = [
  { value: "", labelKey: "mesa.unset" },
  { value: "none", labelKey: "mesa.costNone" },
  { value: "trivial", labelKey: "mesa.costTrivial" },
  { value: "moderate", labelKey: "mesa.costModerate" },
  { value: "high", labelKey: "mesa.costHigh" },
];
const SCOPES: LabeledOpt[] = [
  { value: "", labelKey: "mesa.unset" },
  { value: "entity_only", labelKey: "mesa.scopeEntityOnly" },
  { value: "device_localized", labelKey: "mesa.scopeDeviceLocalized" },
  { value: "room_localized", labelKey: "mesa.scopeRoomLocalized" },
  { value: "zone_wide", labelKey: "mesa.scopeZoneWide" },
  { value: "deployment_wide", labelKey: "mesa.scopeDeploymentWide" },
];
const PRIVACY_LEVELS: LabeledOpt[] = [
  { value: "public", labelKey: "mesa.privPublic" },
  { value: "normal", labelKey: "mesa.privNormal" },
  { value: "sensitive", labelKey: "mesa.privSensitive" },
  { value: "restricted", labelKey: "mesa.privRestricted" },
];
const REVERSIBLE: LabeledOpt[] = [
  { value: "", labelKey: "mesa.unset" },
  { value: "true", labelKey: "mesa.yes" },
  { value: "false", labelKey: "mesa.no" },
];
const ENFORCEMENT_MODES: LabeledOpt[] = [
  { value: "advisory", labelKey: "mesa.modeAdvisory" },
  { value: "enforced", labelKey: "mesa.modeEnforced" },
];

// Scope help is keyed by the scope union so a new level cannot be forgotten;
// the field help below it is a plain map with no such constraint.
const SCOPE_HELP: Record<ProfileScope, string> = {
  entity: "mesa.helpEntity",
  device: "mesa.helpDevice",
  area: "mesa.helpArea",
  integration: "mesa.helpIntegration",
  domain: "mesa.helpDomain",
};

const HELP = {
  tags: "mesa.helpTags",
  control_mode: "mesa.helpControlMode",
  enforcement_mode: "mesa.helpEnforcementMode",
  triggers_automations: "mesa.helpTriggersAutomations",
  reversible: "mesa.helpReversible",
  reversibility_cost: "mesa.helpReversibilityCost",
  side_effect_scope: "mesa.helpSideEffectScope",
  privacy_level: "mesa.helpPrivacyLevel",
};

type ProfileScope = MesaProfileScope;

const SCOPE_LABEL: Record<ProfileScope, string> = {
  entity: "mesa.scopeLabelEntity",
  device: "mesa.scopeLabelDevice",
  area: "mesa.scopeLabelArea",
  integration: "mesa.scopeLabelIntegration",
  domain: "mesa.scopeLabelDomain",
};
const SCOPE_PLACEHOLDER: Record<ProfileScope, string> = {
  entity: "mesa.phEntity",
  device: "mesa.phDevice",
  area: "mesa.phArea",
  integration: "mesa.phIntegration",
  domain: "mesa.phDomain",
};

// One whole sentence per scope. A scope noun is never lowercased or slotted
// into a sentence assembled for English word order.
const CHOOSE_VALID: Record<ProfileScope, string> = {
  entity: "mesa.chooseValidEntity",
  device: "mesa.chooseValidDevice",
  area: "mesa.chooseValidArea",
  integration: "mesa.chooseValidIntegration",
  domain: "mesa.chooseValidDomain",
};
const NO_MATCH: Record<ProfileScope, string> = {
  entity: "mesa.noMatchingEntity",
  device: "mesa.noMatchingDevice",
  area: "mesa.noMatchingArea",
  integration: "mesa.noMatchingIntegration",
  domain: "mesa.noMatchingDomain",
};
const ADD_TITLE: Record<ProfileScope, string> = {
  entity: "mesa.addEntityProfile",
  device: "mesa.addDeviceProfile",
  area: "mesa.addAreaProfile",
  integration: "mesa.addIntegrationProfile",
  domain: "mesa.addDomainProfile",
};
const EDIT_TITLE: Record<ProfileScope, string> = {
  entity: "mesa.editEntityProfile",
  device: "mesa.editDeviceProfile",
  area: "mesa.editAreaProfile",
  integration: "mesa.editIntegrationProfile",
  domain: "mesa.editDomainProfile",
};

// Deleting a cascading profile un-covers everything below it, so the confirm,
// the warning and the button all name the level. Entity deletes cascade to
// nothing and never reach this branch, which is why the union excludes them.
type CascadingScope = Exclude<ProfileScope, "entity">;
const DELETE_CONFIRM: Record<CascadingScope, string> = {
  device: "mesa.deleteDeviceConfirm",
  area: "mesa.deleteAreaConfirm",
  integration: "mesa.deleteIntegrationConfirm",
  domain: "mesa.deleteDomainConfirm",
};
const DELETE_WARN: Record<CascadingScope, string> = {
  device: "mesa.deleteDeviceWarn",
  area: "mesa.deleteAreaWarn",
  integration: "mesa.deleteIntegrationWarn",
  domain: "mesa.deleteDomainWarn",
};
const DELETE_BUTTON: Record<CascadingScope, string> = {
  device: "mesa.deleteDeviceProfile",
  area: "mesa.deleteAreaProfile",
  integration: "mesa.deleteIntegrationProfile",
  domain: "mesa.deleteDomainProfile",
};

// Which inheritance level resolved a field, for display. mesa-core reports the
// raw level name, and an untranslated one dropped into a sentence reads as
// English inside every other locale.
const LEVEL_LABEL: Record<string, string> = {
  entity: "mesa.scopeLabelEntity",
  device: "mesa.scopeLabelDevice",
  area: "mesa.scopeLabelArea",
  integration: "mesa.scopeLabelIntegration",
  domain: "mesa.scopeLabelDomain",
  deployment_default: "mesa.levelDeploymentDefault",
  built_in_baseline: "mesa.levelBuiltInBaseline",
};

function levelText(level: string): string {
  const key = LEVEL_LABEL[level];
  return key ? t(key) : level;
}

function tagsOf(doc: MesaProfileDocument | null): string[] {
  const tags = doc?.semantic_profile?.semantic_tags;
  return Array.isArray(tags) ? (tags as string[]) : [];
}

interface EditorState {
  key: string;
  tags: string[];
  control_mode: string;
  enforcement_mode: string;
  triggers_automations: string;
  reversible: string; // "", "true", "false"
  reversibility_cost: string;
  side_effect_scope: string;
  privacy_level: string;
}

function docToEditor(key: string, doc: MesaProfileDocument | null): EditorState {
  const sp = (doc?.semantic_profile ?? {}) as Record<string, unknown>;
  const ob = (sp.operational_boundaries ?? {}) as Record<string, unknown>;
  const pc = (doc?.privacy_classification ?? {}) as Record<string, unknown>;
  const rev = ob.reversible;
  return {
    key,
    tags: tagsOf(doc),
    control_mode: (ob.control_mode as string) ?? "autonomous",
    enforcement_mode: (ob.enforcement_mode as string) ?? "advisory",
    triggers_automations: (ob.triggers_automations as string) ?? "unknown",
    reversible: rev === true ? "true" : rev === false ? "false" : "",
    reversibility_cost: (ob.reversibility_cost as string) ?? "",
    side_effect_scope: (ob.side_effect_scope as string) ?? "",
    privacy_level: (pc.level as string) ?? "normal",
  };
}

function editorToDoc(s: EditorState): MesaProfileDocument {
  const ob: Record<string, unknown> = {
    control_mode: s.control_mode,
    triggers_automations: s.triggers_automations,
  };
  // Omit when advisory (the default) to keep stored docs clean, matching how
  // mesa-core serialises the field.
  if (s.enforcement_mode === "enforced") ob.enforcement_mode = "enforced";
  if (s.reversible !== "") ob.reversible = s.reversible === "true";
  if (s.reversibility_cost !== "") ob.reversibility_cost = s.reversibility_cost;
  if (s.side_effect_scope !== "") ob.side_effect_scope = s.side_effect_scope;
  return {
    semantic_profile: { semantic_tags: s.tags, operational_boundaries: ob },
    privacy_classification: { level: s.privacy_level },
  };
}

// One endpoint trio per scope, as an exhaustive table rather than an if-chain.
// The chain these replace ended in an untyped `return` for area, so a scope it
// did not name was silently written to the AREA endpoint: adding one to the
// union produced no compile error and no runtime complaint, just a profile
// stored against the wrong level. A Record keyed by the union makes a missing
// scope a build failure instead.
const SCOPE_API: Record<ProfileScope, {
  load: (key: string) => Promise<MesaProfileDocument | null>;
  save: (key: string, doc: MesaProfileDocument) => Promise<MesaValidationIssue[]>;
  remove: (key: string) => Promise<void>;
}> = {
  entity: {
    load: async (k) => (await api.getMesaProfile(k)).stored,
    save: async (k, doc) => (await api.putMesaProfile(k, doc)).warnings,
    remove: async (k) => { await api.deleteMesaProfile(k); },
  },
  device: {
    load: async (k) => (await api.getMesaDevice(k)).stored,
    save: async (k, doc) => { await api.putMesaDevice(k, doc); return []; },
    remove: async (k) => { await api.deleteMesaDevice(k); },
  },
  area: {
    load: async (k) => (await api.getMesaArea(k)).stored,
    save: async (k, doc) => { await api.putMesaArea(k, doc); return []; },
    remove: async (k) => { await api.deleteMesaArea(k); },
  },
  integration: {
    load: async (k) => (await api.getMesaIntegration(k)).stored,
    save: async (k, doc) => { await api.putMesaIntegration(k, doc); return []; },
    remove: async (k) => { await api.deleteMesaIntegration(k); },
  },
  domain: {
    load: async (k) => (await api.getMesaDomain(k)).stored,
    save: async (k, doc) => { await api.putMesaDomain(k, doc); return []; },
    remove: async (k) => { await api.deleteMesaDomain(k); },
  },
};

// Derived from the dispatch table rather than restated, so the two cannot
// disagree. Pinned against the backend's canonical list by a contract test.
export const EDITOR_SCOPES = Object.keys(SCOPE_API) as ProfileScope[];

// The levels that cascade to many entities, in the order the list renders
// them. Entity profiles are absent: they render grouped by domain instead.
export const CASCADING_SCOPES: CascadingScope[] = ["device", "area", "integration", "domain"];

export function loadProfile(scope: ProfileScope, key: string): Promise<MesaProfileDocument | null> {
  return SCOPE_API[scope].load(key);
}

export function saveProfile(scope: ProfileScope, key: string, doc: MesaProfileDocument): Promise<MesaValidationIssue[]> {
  return SCOPE_API[scope].save(key, doc);
}

export function deleteProfile(scope: ProfileScope, key: string): Promise<void> {
  return SCOPE_API[scope].remove(key);
}

// A small "?" badge that reveals brief help on hover/focus. Uses the native
// title attribute so the tooltip is never clipped by the scrolling modal body.
function HelpTip({ text }: { text: string }) {
  return (
    <span className="help-tip" title={text} role="img" aria-label={t("mesa.helpAria", { text })} tabIndex={0}>?</span>
  );
}

function FieldLabel({ id, text, help }: { id?: string; text: string; help: string }) {
  return (
    <label htmlFor={id} className="mesa-field-label">
      {text}
      <HelpTip text={help} />
    </label>
  );
}

// A select rendered with friendly labels but storing slug values, full width so
// every control lines up on the grid.
function SelectField({
  id, label, help, value, options, onChange,
}: { id: string; label: string; help: string; value: string; options: LabeledOpt[]; onChange: (v: string) => void }) {
  return (
    <div className="field">
      <FieldLabel id={id} text={label} help={help} />
      <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{t(o.labelKey)}</option>)}
      </select>
    </div>
  );
}

// Fuzzy combobox over a fixed option set. Selecting an option sets `value` to
// the option's slug; free typing is allowed but the parent validates exactness.
function Combo({
  id, value, options, placeholder, invalid, onChange,
}: {
  id: string;
  value: string;
  options: Opt[];
  placeholder?: string;
  invalid?: boolean;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // What to SHOW for a stored value. The parent stores the option's slug, and
  // for a device that slug is an opaque 32-character registry id, so echoing it
  // back into the input after a pick replaced the name the admin had just
  // chosen with something they cannot read. Falls back to the value itself,
  // which is right for free-typed text and for a slug no longer in the options.
  const labelFor = useCallback(
    (v: string) => options.find((o) => o.value === v)?.label ?? v,
    [options],
  );
  const [query, setQuery] = useState(() => labelFor(value));
  const [active, setActive] = useState(0);
  const listboxId = useId();
  useEffect(() => { setQuery(labelFor(value)); }, [value, labelFor]);

  const matches = useMemo(() => {
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const pool = terms.length === 0
      ? options
      : options.filter((o) => {
          const hay = `${o.value} ${o.label}`.toLowerCase();
          return terms.every((t) => hay.includes(t));
        });
    return pool.slice(0, 10);
  }, [query, options]);

  function pick(v: string) {
    onChange(v);
    setQuery(labelFor(v));
    setActive(0);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, Math.max(matches.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Home" && open) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && open) {
      e.preventDefault();
      setActive(Math.max(matches.length - 1, 0));
    } else if (e.key === "Enter" && open && matches.length > 0) {
      e.preventDefault();
      pick(matches[Math.min(active, matches.length - 1)].value);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="combo">
      <input
        id={id}
        className={`input${invalid ? " error" : ""}`}
        value={query}
        placeholder={placeholder}
        autoComplete="off"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-controls={matches.length > 0 ? listboxId : undefined}
        aria-activedescendant={open && matches.length > 0 ? `${listboxId}-option-${Math.min(active, matches.length - 1)}` : undefined}
        onChange={(e) => { setQuery(e.target.value); onChange(e.target.value); setOpen(true); setActive(0); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={handleKeyDown}
      />
      {open && matches.length > 0 && (
        <ul className="combo-list" role="listbox" id={listboxId}>
          {matches.map((o, i) => (
            <li
              key={o.value}
              className="combo-option"
              id={`${listboxId}-option-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => { e.preventDefault(); pick(o.value); }}
              onMouseEnter={() => setActive(i)}
            >
              <span className="combo-option-label">{o.label}</span>
              {o.label !== o.value && <code className="combo-option-sub">{o.value}</code>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Effective (inherited) control + enforcement mode from a resolved profile detail,
// preferring the explanation's resolved value, then the effective doc, then the
// mesa-core defaults. Drives the "Effective"/"Currently" panel and the pre-fill of
// a new entity profile.
function effectiveModes(detail: MesaProfileDetail): { control_mode: string; enforcement_mode: string; cmLevel?: string; enLevel?: string } {
  const exps = detail.explanation?.explanation ?? [];
  const find = (suffix: string) => exps.find((e) => e.field_path.endsWith(suffix));
  const ob = (detail.effective?.semantic_profile as { operational_boundaries?: Record<string, unknown> } | undefined)?.operational_boundaries ?? {};
  const cm = find("control_mode");
  const en = find("enforcement_mode");
  return {
    control_mode: String(cm?.effective_value ?? ob.control_mode ?? "autonomous"),
    enforcement_mode: String(en?.effective_value ?? ob.enforcement_mode ?? "advisory"),
    cmLevel: cm?.provided_by_level,
    enLevel: en?.provided_by_level,
  };
}

// Shows an entity's EFFECTIVE resolved control_mode/enforcement and which layer
// provides each. For an existing profile it flags when a broader domain/area
// profile overrides the entity-level setting (most-restrictive-wins); for a new
// profile (creating) it explains that the fields below are pre-filled to match.
function MesaEffectivePanel({ detail, creating }: { detail: MesaProfileDetail; creating?: boolean }) {
  const { control_mode: cmVal, enforcement_mode: enVal, cmLevel, enLevel } = effectiveModes(detail);
  const overridden = (cmLevel && cmLevel !== "entity") || (enLevel && enLevel !== "entity");
  return (
    <div className="mesa-effective">
      <span className="mesa-effective-title">{creating ? t("mesa.currently") : t("mesa.effective")}</span>
      <span>
        {tRich("mesa.effControlMode", { code: (c) => <code>{c}</code> }, { mode: cmVal })}
        {cmLevel && tRich("mesa.effFrom", { em: (c) => <em>{c}</em> }, { level: levelText(cmLevel) })}
        {tRich("mesa.effEnforcement", { code: (c) => <code>{c}</code> }, { mode: enVal })}
        {enLevel && tRich("mesa.effFrom", { em: (c) => <em>{c}</em> }, { level: levelText(enLevel) })}
      </span>
      {creating ? (
        <div className="mesa-effective-note">
          {t("mesa.effectiveNoteCreating")}
        </div>
      ) : overridden ? (
        <div className="mesa-effective-note">
          {t("mesa.effectiveNoteOverridden")}
        </div>
      ) : null}
    </div>
  );
}

export function ProfileEditor({
  scope,
  profileKey,
  isNew,
  entityTree,
  canonicalTags,
  integrationOptions,
  deviceOptions,
  onClose,
  onSaved,
  lockedKey,
  keyLabel,
  seedControlMode,
}: {
  scope: ProfileScope;
  profileKey: string | null;
  isNew: boolean;
  entityTree: EntityTreeData | null;
  canonicalTags: string[];
  // Installed integrations (id = component name, name = friendly title) for the
  // integration-scope picker. Only the MESA tab supplies these.
  integrationOptions?: { id: string; name: string }[];
  // Supplied by the MESA tab only, to populate the picker. The in-context
  // injector always passes a lockedKey and so needs no picker source; it
  // supplies keyLabel instead, which is what NAMES the locked target.
  deviceOptions?: { id: string; name: string }[];
  onClose: () => void;
  onSaved: () => void;
  // When true, the target id is fixed (supplied by the caller, e.g. the in-context
  // injector) rather than picked from the registry. Hides the combobox and skips
  // its validation, so entities not in the registry (e.g. "unmanageable" ones) work.
  lockedKey?: boolean;
  // What to CALL profileKey in the title, the locked target field and the
  // delete warning. A device key is an opaque 32-character registry id, so
  // rendering it raw names nothing an operator recognises; every caller that
  // knows the display name passes it here. Absent falls back to the key, which
  // is right for domain scope (its key is its name) and for a profile whose
  // target the registry no longer knows.
  keyLabel?: string;
  // Initial control_mode for a NEW profile, overriding the effective-mode seed.
  // Used by the suggestions Review flow so the editor opens at the suggested mode.
  seedControlMode?: string;
}) {
  const initialState = () => {
    const base = docToEditor(profileKey ?? "", null);
    if (isNew && seedControlMode) base.control_mode = seedControlMode;
    return base;
  };
  const [detail, setDetail] = useState<MesaProfileDetail | null>(null);
  const [state, setState] = useState<EditorState>(initialState);
  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<MesaValidationIssue[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [showReco, setShowReco] = useState(false);
  // Snapshot of the last persisted (or freshly initialised) state, for the
  // unsaved-changes guard.
  const cleanSnapshot = useRef<string>(JSON.stringify(initialState()));
  // Entity key already pre-filled from effective, so a re-render does not re-seed
  // and clobber the user's edits.
  const seededForKey = useRef<string | null>(null);

  useEffect(() => {
    if (isNew || !profileKey) return;
    setLoading(true);
    Promise.all([
      loadProfile(scope, profileKey),
      // Effective resolution only makes sense for entities.
      scope === "entity" ? api.getMesaProfile(profileKey) : Promise.resolve(null),
    ])
      .then(([stored, d]) => {
        setDetail(d);
        const next = docToEditor(profileKey, stored);
        setState(next);
        cleanSnapshot.current = JSON.stringify(next);
      })
      .catch((e) => setError(e instanceof Error ? e.message : t("mesa.loadProfileFailed")))
      .finally(() => setLoading(false));
  }, [scope, profileKey, isNew]);

  function set<K extends keyof EditorState>(key: K, value: EditorState[K]) {
    setState((s) => ({ ...s, [key]: value }));
  }

  // Valid keys for the current scope, derived from the live registry.
  const keyOptions = useMemo<Opt[]>(() => {
    if (scope === "device") {
      return (deviceOptions ?? [])
        .map((d) => ({ value: d.id, label: d.name }))
        .sort((a, b) => compareStrings(a.label, b.label));
    }
    if (scope === "integration") {
      return (integrationOptions ?? [])
        .map((i) => ({ value: i.id, label: i.name && i.name !== i.id ? `${i.name} (${i.id})` : i.id }))
        .sort((a, b) => compareStrings(a.label, b.label));
    }
    if (!entityTree) return [];
    if (scope === "domain") {
      return Object.keys(entityTree).sort().map((d) => ({ value: d, label: d }));
    }
    if (scope === "area") {
      const seen = new Map<string, string>();
      for (const dt of Object.values(entityTree)) {
        for (const info of Object.values(dt.entity_details)) {
          if (info.area_id && !seen.has(info.area_id)) seen.set(info.area_id, info.area_name || info.area_id);
        }
      }
      return [...seen.entries()].map(([value, label]) => ({ value, label })).sort((a, b) => compareStrings(a.label, b.label));
    }
    const out: Opt[] = [];
    for (const dt of Object.values(entityTree)) {
      for (const [eid, info] of Object.entries(dt.entity_details)) {
        out.push({ value: eid, label: info.friendly_name || eid });
      }
    }
    return out.sort((a, b) => compareStrings(a.label, b.label));
  }, [entityTree, scope, integrationOptions, deviceOptions]);

  // New entity profiles: seed control + enforcement from the entity's effective
  // (inherited) mode, so creating a profile starts from what MESA already applies
  // (e.g. a lock opens at "prohibited") rather than a generic Autonomous default.
  // Seeds once per selected entity and never overwrites a value the user then edits.
  useEffect(() => {
    if (!isNew || scope !== "entity") return;
    const key = state.key.trim();
    if (!key || !keyOptions.some((o) => o.value === key)) {
      setDetail(null);
      seededForKey.current = null;
      return;
    }
    if (seededForKey.current === key) return;
    seededForKey.current = key;
    let cancelled = false;
    api.getMesaProfile(key)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        const eff = effectiveModes(d);
        setState((s) => {
          // A caller-supplied seed (suggestions Review) wins over the
          // effective-mode default; enforcement still seeds from effective.
          const next = {
            ...s,
            control_mode: seedControlMode ?? eff.control_mode,
            enforcement_mode: eff.enforcement_mode,
          };
          cleanSnapshot.current = JSON.stringify(next);
          return next;
        });
      })
      .catch(() => { if (!cancelled) setDetail(null); });
    return () => { cancelled = true; };
  }, [isNew, scope, state.key, keyOptions, seedControlMode]);

  // Integration: validate against the picker list when it loaded; if that list is
  // empty (e.g. the integration-options endpoint isn't reachable yet), fall back to
  // validating the typed component-name format so the field is never a dead end.
  const keyValid = !!lockedKey || !isNew
    || (scope === "integration" && keyOptions.length === 0
      ? /^[a-z][a-z0-9_]*$/.test(state.key.trim())
      : keyOptions.some((o) => o.value === state.key.trim()));
  const keyInvalidShown = !lockedKey && isNew && state.key.trim() !== "" && !keyValid;
  const dirty = JSON.stringify(state) !== cleanSnapshot.current;
  const canSave = !saving && !loading && keyValid;

  // Suggested tags for this scope's domain, ordered by root priority and
  // interleaved so the most relevant namespace leads (not alphabetical, which
  // would let an early root like "automation" crowd out "lighting").
  const recommendedTags = useMemo(() => {
    const domain = scope === "entity" ? state.key.split(".")[0] : scope === "domain" ? state.key.trim() : "";
    let roots = DOMAIN_TAG_ROOTS[domain] ?? FALLBACK_TAG_ROOTS;
    let byRoot = roots.map((r) => canonicalTags.filter((t) => t.split(".")[0] === r));
    if (byRoot.every((l) => l.length === 0)) {
      roots = FALLBACK_TAG_ROOTS;
      byRoot = roots.map((r) => canonicalTags.filter((t) => t.split(".")[0] === r));
    }
    const out: string[] = [];
    for (let col = 0; out.length < 8; col++) {
      let advanced = false;
      for (const list of byRoot) {
        if (list[col]) { out.push(list[col]); advanced = true; if (out.length >= 8) break; }
      }
      if (!advanced) break;
    }
    return out;
  }, [scope, state.key, canonicalTags]);

  function attemptClose() {
    if (dirty) { setConfirmDiscard(true); return; }
    onClose();
  }

  async function save() {
    if (!keyValid) { setError(t(CHOOSE_VALID[scope])); return; }
    setSaving(true);
    setError(null);
    try {
      const w = await saveProfile(scope, state.key.trim(), editorToDoc(state));
      cleanSnapshot.current = JSON.stringify(state);
      setWarnings(w);
      if (w.length === 0) { onSaved(); onClose(); }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("mesa.saveProfileFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!profileKey) return;
    setSaving(true);
    try {
      await deleteProfile(scope, profileKey);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.deleteProfileFailed"));
      setSaving(false);
    }
  }

  const titleVerb = isNew
    ? t(ADD_TITLE[scope])
    : t(EDIT_TITLE[scope], { key: keyLabel || String(profileKey) });

  return (
    <>
    <Modal titleId="mesa-editor-title" onClose={attemptClose}>
      <h3 className="modal-title" id="mesa-editor-title">{titleVerb}</h3>
      <div className="mesa-editor-body">
        {error && <ErrorMsg msg={error} />}
        {loading ? <Loading /> : (
          <>
            {scope === "entity" && !isNew && detail && <MesaEffectivePanel detail={detail} />}
            {isNew && !lockedKey && (
              <div className="field">
                <FieldLabel id="mesa-key" text={t(SCOPE_LABEL[scope])} help={t(SCOPE_HELP[scope])} />
                <Combo
                  id="mesa-key"
                  value={state.key}
                  options={keyOptions}
                  placeholder={t(SCOPE_PLACEHOLDER[scope])}
                  invalid={keyInvalidShown}
                  onChange={(v) => set("key", v)}
                />
                {keyInvalidShown && (
                  <span className="field-error">{t(NO_MATCH[scope])}</span>
                )}
              </div>
            )}
            {isNew && lockedKey && (
              <div className="field">
                <FieldLabel id="mesa-key" text={t(SCOPE_LABEL[scope])} help={t(SCOPE_HELP[scope])} />
                <input id="mesa-key" className="input" value={keyLabel || state.key} readOnly disabled />
              </div>
            )}

            {isNew && scope === "entity" && detail && <MesaEffectivePanel detail={detail} creating />}

            <div className="field">
              <div className="mesa-taglabel-row">
                <FieldLabel id="mesa-tags" text={t("mesa.semanticTags")} help={t(HELP.tags)} />
                {recommendedTags.length > 0 && (
                  <button type="button" className="link-btn" onClick={() => setShowReco((s) => !s)}>
                    {showReco ? t("mesa.hideSuggestions") : t("mesa.showSuggestions")}
                  </button>
                )}
              </div>
              <TagInput
                value={state.tags}
                onChange={(t) => set("tags", t)}
                canonicalTags={canonicalTags}
                recommended={recommendedTags}
                showRecommended={showReco}
              />
            </div>

            <div className="mesa-grid">
              <SelectField id="mesa-cm" label={t("mesa.fieldControlMode")} help={t(HELP.control_mode)}
                value={state.control_mode} options={CONTROL_MODES} onChange={(v) => set("control_mode", v)} />
              <SelectField id="mesa-em" label={t("mesa.fieldEnforcement")} help={t(HELP.enforcement_mode)}
                value={state.enforcement_mode} options={ENFORCEMENT_MODES} onChange={(v) => set("enforcement_mode", v)} />
              <SelectField id="mesa-ta" label={t("mesa.fieldTriggersAutomations")} help={t(HELP.triggers_automations)}
                value={state.triggers_automations} options={TRIGGERS} onChange={(v) => set("triggers_automations", v)} />
              <SelectField id="mesa-rev" label={t("mesa.fieldReversible")} help={t(HELP.reversible)}
                value={state.reversible} options={REVERSIBLE} onChange={(v) => set("reversible", v)} />
              <SelectField id="mesa-rc" label={t("mesa.fieldReversibilityCost")} help={t(HELP.reversibility_cost)}
                value={state.reversibility_cost} options={REVERSIBILITY_COSTS} onChange={(v) => set("reversibility_cost", v)} />
              <SelectField id="mesa-ses" label={t("mesa.fieldSideEffectScope")} help={t(HELP.side_effect_scope)}
                value={state.side_effect_scope} options={SCOPES} onChange={(v) => set("side_effect_scope", v)} />
              <SelectField id="mesa-pl" label={t("mesa.fieldPrivacyLevel")} help={t(HELP.privacy_level)}
                value={state.privacy_level} options={PRIVACY_LEVELS} onChange={(v) => set("privacy_level", v)} />
            </div>

            {warnings.length > 0 && (
              <div className="banner banner-warn">
                <strong>{t("mesa.savedWithWarnings")}</strong>
                <ul>
                  {warnings.map((w, i) => (
                    <li key={i}>{t("mesa.warningRow", { recommendation: w.recommendation, automationId: w.automation_id, role: w.role })}</li>
                  ))}
                </ul>
                <button className="btn btn-sm" onClick={() => { onSaved(); onClose(); }}>{t("mesa.dismiss")}</button>
              </div>
            )}

            {confirmDelete && scope !== "entity" && (
              <div className="banner banner-warn">
                <strong>{t(DELETE_CONFIRM[scope])}</strong>
                <p>{t(DELETE_WARN[scope], { key: keyLabel || String(profileKey) })}</p>
                <div className="modal-actions">
                  <button className="btn btn-ghost btn-sm" onClick={() => setConfirmDelete(false)} disabled={saving}>{t("mesa.cancel")}</button>
                  <button className="btn btn-danger btn-sm" onClick={remove} disabled={saving}>
                    {saving ? t("mesa.deleting") : t(DELETE_BUTTON[scope])}
                  </button>
                </div>
              </div>
            )}

            {/* Resolution warnings say that a rule fired and changed the outcome:
                an override that was ignored, a malformed field that was dropped,
                a vendor-declared capability that overrode an operator decision.
                They arrive on every profile read and were displayed nowhere, so
                an operator whose deliberate setting did not take effect had no
                signal at all. The text is mesa-core's own and stays English, like
                the resolution table below it. */}
            {!isNew && scope === "entity" && detail && detail.explanation.warnings.length > 0 && (
              <div className="banner banner-warn mesa-resolution-warnings">
                <strong>{t("mesa.resolutionWarnings")}</strong>
                <ul>
                  {detail.explanation.warnings.map((w) => <li key={w}>{w}</li>)}
                </ul>
              </div>
            )}

            {!isNew && scope === "entity" && detail && (
              <details className="mesa-explain">
                <summary>{t("mesa.effectiveResolution")} <HelpTip text={t("mesa.helpEffectiveResolution")} /></summary>
                <table className="data-table">
                  <thead><tr><th>{t("mesa.colField")}</th><th>{t("mesa.colEffective")}</th><th>{t("mesa.colFrom")}</th><th>{t("mesa.colOrigin")}</th></tr></thead>
                  <tbody>
                    {detail.explanation.explanation.map((row) => (
                      <tr key={row.field_path}>
                        <td><code>{row.field_path}</code></td>
                        <td>{String(row.effective_value)}</td>
                        <td>{levelText(row.provided_by_level)}</td>
                        <td>{row.provided_by_origin}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            )}
          </>
        )}
      </div>
      <div className="modal-actions">
        {!isNew && !confirmDelete && (
          <button
            className="btn btn-danger"
            onClick={() => (scope === "entity" ? remove() : setConfirmDelete(true))}
            disabled={saving}
          >
            {t("mesa.delete")}
          </button>
        )}
        <button className="btn btn-ghost" onClick={attemptClose} disabled={saving}>{t("mesa.cancel")}</button>
        <button className="btn btn-primary" onClick={save} disabled={!canSave}>
          {saving ? t("mesa.saving") : t("mesa.save")}
        </button>
      </div>
    </Modal>
    {confirmDiscard && (
      <Modal titleId="mesa-discard-title" onClose={() => setConfirmDiscard(false)}>
        <h3 className="modal-title" id="mesa-discard-title">{t("mesa.discardTitle")}</h3>
        <p className="modal-body-text">{t("mesa.discardBody")}</p>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={() => setConfirmDiscard(false)}>{t("mesa.keepEditing")}</button>
          <button className="btn btn-danger" onClick={onClose}>{t("mesa.discardChanges")}</button>
        </div>
      </Modal>
    )}
    </>
  );
}

// Control-mode display metadata, reusing the shared badge palette.
const CONTROL_MODE_META: Record<string, { labelKey: string; cls: string }> = {
  autonomous: { labelKey: "mesa.modeAutonomous", cls: "badge-green" },
  confirm: { labelKey: "mesa.modeConfirm", cls: "badge-amber" },
  read_only: { labelKey: "mesa.modeReadOnly", cls: "badge-grey" },
  prohibited: { labelKey: "mesa.modeProhibited", cls: "badge-red" },
};

function rawControlMode(doc: MesaProfileDocument | null): string {
  const ob = (doc?.semantic_profile?.operational_boundaries ?? {}) as Record<string, unknown>;
  return (ob.control_mode as string) ?? "inherited";
}

function isEnforced(doc: MesaProfileDocument | null): boolean {
  const ob = (doc?.semantic_profile?.operational_boundaries ?? {}) as Record<string, unknown>;
  return ob.enforcement_mode === "enforced";
}

// Provenance of a stored profile. "developer" means it was imported from an
// integration's mesa_profile.json sidecar (a vendor-supplied profile). The
// serialized document nests metadata_origin under semantic_profile (matching
// SemanticProfile.to_dict); a top-level copy is tolerated as a fallback.
function profileSource(doc: MesaProfileDocument | null): string {
  const sp = (doc?.semantic_profile ?? {}) as Record<string, unknown>;
  const mo = (sp.metadata_origin ?? doc?.metadata_origin ?? {}) as { source?: string };
  return mo.source ?? "";
}

function domainOf(entityId: string): string {
  return entityId.split(".")[0] || "other";
}

function ControlBadge({ mode }: { mode: string }) {
  const meta = CONTROL_MODE_META[mode] ?? { labelKey: "mesa.modeInherited", cls: "badge-grey" };
  return <span className={`badge ${meta.cls}`}>{t(meta.labelKey)}</span>;
}

// Export flow: confirm-gated download of the full mesa-core portability
// archive. Not destructive, but the file is the deployment's whole safety
// policy, so the modal says exactly what leaves the browser.
export function ExportModal({ profileCount, onClose }: { profileCount: number; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function doExport() {
    setBusy(true);
    setError(null);
    try {
      const archive = await api.exportMesaProfiles();
      const blob = new Blob([JSON.stringify(archive, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `phoenix-mcp-mesa-profiles-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("mesa.exportFailed"));
      setBusy(false);
    }
  }

  return (
    <Modal titleId="mesa-export-title" onClose={onClose}>
      <h3 className="modal-title" id="mesa-export-title">{t("mesa.exportTitle")}</h3>
      {error && <ErrorMsg msg={error} />}
      <p className="modal-body-text">
        {tn("mesa.exportBody", profileCount)}
      </p>
      <p className="modal-body-text">
        {t("mesa.exportNote")}
      </p>
      <div className="modal-actions">
        <button className="btn btn-ghost" onClick={onClose} disabled={busy}>{t("mesa.cancel")}</button>
        <button className="btn btn-primary" onClick={doExport} disabled={busy}>
          {busy ? t("mesa.exporting") : t("mesa.export")}
        </button>
      </div>
    </Modal>
  );
}

type ImportCounts = { entities: number; devices: number; domains: number; integrations: number; areas: number; defaults: boolean };

// Import flow: pick a file, see what it contains, choose the conflict policy,
// then confirm. Default is skip (existing profiles are never touched); the
// "replace" checkbox turns the confirm explicitly destructive.
export function ImportModal({ onClose }: { onClose: (didImport: boolean) => void }) {
  const [archive, setArchive] = useState<MesaExportArchive | null>(null);
  const [counts, setCounts] = useState<ImportCounts | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MesaImportResult | null>(null);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    setArchive(null);
    setCounts(null);
    setParseError(null);
    setError(null);
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text()) as MesaExportArchive;
      const inner = parsed?.mesa_export;
      if (!inner || typeof inner !== "object") {
        setParseError(t("mesa.importNotArchive"));
        return;
      }
      setArchive(parsed);
      setCounts({
        entities: Object.keys(inner.entities ?? {}).length,
        devices: Object.keys(inner.devices ?? {}).length,
        domains: Object.keys(inner.domains ?? {}).length,
        integrations: Object.keys(inner.integrations ?? {}).length,
        areas: Object.keys(inner.areas ?? {}).length,
        defaults: inner.deployment_defaults != null,
      });
    } catch {
      setParseError(t("mesa.importNotJson"));
    }
  }

  async function doImport() {
    if (!archive) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.importMesaProfiles(archive, replaceExisting ? "overwrite" : "skip"));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("mesa.importFailed"));
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    const invalidEntries = Object.entries(result.invalid);
    return (
      <Modal titleId="mesa-import-title" onClose={() => onClose(true)}>
        <h3 className="modal-title" id="mesa-import-title">{t("mesa.importComplete")}</h3>
        <p className="modal-body-text">
          {tn("mesa.importResult", result.imported, { overwritten: result.overwritten, skipped: result.skipped_existing.length })}
        </p>
        {invalidEntries.length > 0 && (
          <div className="banner banner-warn">
            <strong>{t("mesa.importInvalid", { count: invalidEntries.length })}</strong>
            <ul>
              {invalidEntries.map(([key, msg]) => (
                <li key={key}><code>{key}</code>: {msg}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={() => onClose(true)}>{t("common.close")}</button>
        </div>
      </Modal>
    );
  }

  const summary = counts
    ? [
        counts.entities && t("mesa.countEntity", { count: counts.entities }),
        counts.devices && t("mesa.countDevice", { count: counts.devices }),
        counts.domains && t("mesa.countDomain", { count: counts.domains }),
        counts.integrations && t("mesa.countIntegration", { count: counts.integrations }),
        counts.areas && t("mesa.countArea", { count: counts.areas }),
      ].filter(Boolean).join(t("common.listSeparator"))
    : "";

  return (
    <Modal titleId="mesa-import-title" onClose={() => onClose(false)}>
      <h3 className="modal-title" id="mesa-import-title">{t("mesa.importTitle")}</h3>
      {error && <ErrorMsg msg={error} />}
      <p className="modal-body-text">
        {t("mesa.importIntro")}
      </p>
      <div className="field">
        <input
          type="file"
          accept="application/json,.json"
          aria-label={t("mesa.archiveFileAria")}
          onChange={onFile}
        />
        {parseError && <span className="field-error">{parseError}</span>}
        {counts && (
          <p className="modal-body-text" style={{ margin: "8px 0 0" }}>
            {counts.defaults
              ? t("mesa.archiveContainsDefaults", { summary: summary || t("mesa.archiveNone") })
              : t("mesa.archiveContains", { summary: summary || t("mesa.archiveNone") })}
          </p>
        )}
      </div>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={replaceExisting}
          onChange={(e) => setReplaceExisting(e.target.checked)}
        />
        <span>{t("mesa.replaceExisting")}</span>
      </label>
      {replaceExisting ? (
        <div className="banner banner-warn">
          {t("mesa.replaceWarn")}
        </div>
      ) : (
        <p className="modal-body-text">
          {t("mesa.skipNote")}
        </p>
      )}
      <div className="modal-actions">
        <button className="btn btn-ghost" onClick={() => onClose(false)} disabled={busy}>{t("mesa.cancel")}</button>
        <button
          className={`btn ${replaceExisting ? "btn-danger" : "btn-primary"}`}
          onClick={doImport}
          disabled={busy || !archive}
        >
          {busy ? t("mesa.importing") : replaceExisting ? t("mesa.importAndReplace") : t("mesa.import")}
        </button>
      </div>
    </Modal>
  );
}

type Editing = { scope: ProfileScope; key: string | null; isNew: boolean; seedControlMode?: string; lockedKey?: boolean };

export function MesaView() {
  const [profiles, setProfiles] = useState<MesaProfileListItem[]>([]);
  const [issues, setIssues] = useState<MesaIssuesResponse>({ issues: [], orphans: [], orphan_devices: [], orphan_areas: [], orphan_integrations: [], suggestions: [], dismissed_suggestions: [] });
  const [entityTree, setEntityTree] = useState<EntityTreeData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [domains, setDomains] = useState<{ domain: string; document: MesaProfileDocument }[]>([]);
  const [integrations, setIntegrations] = useState<{ integration: string; document: MesaProfileDocument }[]>([]);
  const [areas, setAreas] = useState<{ area_id: string; document: MesaProfileDocument }[]>([]);
  const [devices, setDevices] = useState<{ device_id: string; document: MesaProfileDocument }[]>([]);
  const [canonicalTags, setCanonicalTags] = useState<string[]>([]);
  const [integrationOptions, setIntegrationOptions] = useState<{ id: string; name: string }[]>([]);
  const [deviceOptions, setDeviceOptions] = useState<{ id: string; name: string }[]>([]);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");  // "" = all; a control_mode value; or "enforced"
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [confirmClearOrphans, setConfirmClearOrphans] = useState(false);
  const [clearingOrphans, setClearingOrphans] = useState(false);
  const [showExport, setShowExport] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [suggestBusyKey, setSuggestBusyKey] = useState<string | null>(null);
  const [rescanning, setRescanning] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, iss, doms, ints, ars, devs] = await Promise.all([
        api.listMesaProfiles({ limit: 200 }),
        // Suggestions-scoped recompute: refresh() runs after every profile
        // save/delete in this tab (including Apply/Review from a suggestion),
        // and a saved profile's coverage must be re-evaluated immediately or
        // the entity lingers in the suggestions list until something else
        // happens to recompute it. Trigger issues/orphans stay cached here
        // (unaffected by an unrelated profile save); the Suggestions card's
        // own Rescan button is the only way to force-refresh those.
        api.getMesaIssues("suggestions"),
        api.listMesaDomains().catch(() => ({ domains: [] })),
        api.listMesaIntegrations().catch(() => ({ integrations: [] })),
        api.listMesaAreas().catch(() => ({ areas: [] })),
        api.listMesaDevices().catch(() => ({ devices: [] })),
      ]);
      setProfiles(list.profiles);
      setIssues(iss);
      setDomains(doms.domains);
      setIntegrations(ints.integrations);
      setAreas(ars.areas);
      setDevices(devs.devices);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, []);

  const clearOrphans = useCallback(async () => {
    setClearingOrphans(true);
    setError(null);
    try {
      await api.clearMesaOrphans();
      setConfirmClearOrphans(false);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.clearOrphansFailed"));
    } finally {
      setClearingOrphans(false);
    }
  }, [refresh]);

  // Apply a suggestion as-is: create the profile with the suggested mode, the
  // reason preserved as control_reason so future admins (and agents, via
  // authored_restrictions) see why it exists. Never automatic; this is the
  // admin's click.
  const applySuggestion = useCallback(async (s: import("../types").MesaSuggestion) => {
    setSuggestBusyKey(s.key);
    setError(null);
    try {
      const doc: MesaProfileDocument = {
        semantic_profile: {
          operational_boundaries: { control_mode: s.suggested_mode, control_reason: s.reason },
        },
      };
      if (s.scope === "domain") await api.putMesaDomain(s.subject_id, doc);
      else await api.putMesaProfile(s.subject_id, doc);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.applySuggestionFailed"));
    } finally {
      setSuggestBusyKey(null);
    }
  }, [refresh]);

  const dismissSuggestion = useCallback(async (s: import("../types").MesaSuggestion) => {
    setSuggestBusyKey(s.key);
    setError(null);
    try {
      const resp = await api.dismissMesaSuggestion(s.key);
      setIssues((prev) => ({ ...prev, suggestions: resp.suggestions, dismissed_suggestions: resp.dismissed_suggestions }));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.dismissSuggestionFailed"));
    } finally {
      setSuggestBusyKey(null);
    }
  }, []);

  const restoreAllSuggestions = useCallback(async () => {
    setError(null);
    try {
      const resp = await api.restoreMesaSuggestions({ all: true });
      setIssues((prev) => ({ ...prev, suggestions: resp.suggestions, dismissed_suggestions: resp.dismissed_suggestions }));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.restoreSuggestionsFailed"));
    }
  }, []);

  const rescanSuggestions = useCallback(async () => {
    setRescanning(true);
    setError(null);
    try {
      setIssues(await api.getMesaIssues("suggestions"));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("mesa.rescanFailed"));
    } finally {
      setRescanning(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  // Load the registry once for the editor's fuzzy key search + validation, and
  // so the list can search/show friendly names.
  useEffect(() => { api.getEntityTree().then(setEntityTree).catch(() => null); }, []);
  // The canonical MESA tag vocabulary powers the tag-input autocomplete.
  useEffect(() => { api.getMesaVocabulary().then((v) => setCanonicalTags(v.canonical_tags)).catch(() => null); }, []);
  // Installed integrations (those with entities) for the integration-profile picker.
  useEffect(() => { api.getMesaIntegrationOptions().then((r) => setIntegrationOptions(r.integrations)).catch(() => null); }, []);
  // Registered devices for the device-profile picker. A device id is opaque,
  // so unlike an area there is nothing an admin could type from memory.
  useEffect(() => { api.getMesaDeviceOptions().then((r) => setDeviceOptions(r.devices)).catch(() => null); }, []);

  const friendly = useCallback((eid: string): string => {
    return entityTree?.[domainOf(eid)]?.entity_details[eid]?.friendly_name ?? "";
  }, [entityTree]);

  // What to CALL a cascading-scope profile key, per scope.
  //
  // A device key is an opaque 32-character registry id, so a row or a modal
  // title showing the raw key names nothing an operator can recognise: that is
  // the whole reason this exists. Area and integration keys are readable slugs
  // rather than opaque, but their registries carry a real display name, so
  // resolving those through the same map keeps one rule instead of a
  // device-only special case. Domain is deliberately absent: its key IS its
  // name, so there is nothing to resolve.
  //
  // Built from the same sources the pickers already load, so no extra request.
  // A key the registry does not know is simply absent, and every caller falls
  // back to the key itself, which is what keeps an ORPHANED profile (its device
  // or area deleted) readable and therefore deletable.
  const scopeKeyName = useMemo(() => {
    const areaNames = new Map<string, string>();
    for (const dt of Object.values(entityTree ?? {})) {
      for (const info of Object.values(dt.entity_details)) {
        if (info.area_id && info.area_name) areaNames.set(info.area_id, info.area_name);
      }
    }
    const byScope: Partial<Record<CascadingScope, Map<string, string>>> = {
      device: new Map(deviceOptions.map((d) => [d.id, d.name])),
      integration: new Map(integrationOptions.map((i) => [i.id, i.name])),
      area: areaNames,
    };
    return (scope: CascadingScope, key: string): string => byScope[scope]?.get(key) || key;
  }, [entityTree, deviceOptions, integrationOptions]);

  // Cascading-rule profiles (device, area, integration, domain) render as collapsible
  // cards mirroring the per-domain entity groups. Sentinel collapse keys carry a
  // "scope:" prefix so they never collide with a real domain group key.
  const scopeRows: Record<CascadingScope, { key: string; document: MesaProfileDocument }[]> = useMemo(() => ({
    device: devices.map((d) => ({ key: d.device_id, document: d.document })),
    area: areas.map((a) => ({ key: a.area_id, document: a.document })),
    integration: integrations.map((i) => ({ key: i.integration, document: i.document })),
    domain: domains.map((d) => ({ key: d.domain, document: d.document })),
  }), [devices, areas, integrations, domains]);
  const scopeDefs = useMemo(() => CASCADING_SCOPES.map((scope) => ({
    titleKey: SCOPE_LABEL[scope],
    sentinel: `scope:${scope}`,
    scope: scope as ProfileScope,
    rows: scopeRows[scope],
  })), [scopeRows]);

  // Manage-by-exception summary: tally control modes (+ enforced) across ALL
  // profiles, entity and cascading, so the filter pills cover both.
  const counts = useMemo(() => {
    const c: Record<string, number> = { autonomous: 0, confirm: 0, read_only: 0, prohibited: 0, inherited: 0, enforced: 0 };
    const tally = (doc: MesaProfileDocument) => {
      const m = rawControlMode(doc);
      c[m] = (c[m] ?? 0) + 1;
      if (isEnforced(doc)) c.enforced += 1;
    };
    for (const p of profiles) tally(p.document);
    for (const def of scopeDefs) for (const r of def.rows) tally(r.document);
    return c;
  }, [profiles, scopeDefs]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return profiles.filter((p) => {
      if (filter === "enforced") { if (!isEnforced(p.document)) return false; }
      else if (filter && rawControlMode(p.document) !== filter) return false;
      if (!q) return true;
      const hay = `${p.entity_id} ${friendly(p.entity_id)} ${tagsOf(p.document).join(" ")}`.toLowerCase();
      return hay.includes(q);
    });
  }, [profiles, search, filter, friendly]);

  // The control-mode pills and search box filter the cascading-rule cards too.
  const scopeFiltered = useMemo(() => {
    const q = search.trim().toLowerCase();
    // The display NAME is searchable alongside the key, so a device is findable
    // by what it is called rather than only by its opaque registry id.
    const rowOk = (doc: MesaProfileDocument, key: string, name: string) => {
      if (filter === "enforced") { if (!isEnforced(doc)) return false; }
      else if (filter && rawControlMode(doc) !== filter) return false;
      if (!q) return true;
      return `${key} ${name} ${tagsOf(doc).join(" ")}`.toLowerCase().includes(q);
    };
    return scopeDefs
      .map((d) => ({ ...d, rows: d.rows.filter((r) => rowOk(r.document, r.key, scopeKeyName(d.scope as CascadingScope, r.key))) }))
      .filter((d) => d.rows.length > 0);
  }, [scopeDefs, search, filter, scopeKeyName]);

  // Group filtered profiles by domain; gated entities float to the top of each group.
  const groups = useMemo(() => {
    const m = new Map<string, MesaProfileListItem[]>();
    for (const p of filtered) {
      const d = domainOf(p.entity_id);
      const arr = m.get(d);
      if (arr) arr.push(p); else m.set(d, [p]);
    }
    for (const arr of m.values()) {
      arr.sort((a, b) => {
        const aAuto = rawControlMode(a.document) === "autonomous" ? 1 : 0;
        const bAuto = rawControlMode(b.document) === "autonomous" ? 1 : 0;
        return aAuto - bAuto || compareStrings(a.entity_id, b.entity_id);
      });
    }
    return [...m.entries()].sort((a, b) => compareStrings(a[0], b[0]));
  }, [filtered]);

  function toggleGroup(d: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d); else next.add(d);
      return next;
    });
  }

  const chips = [
    { key: "confirm", labelKey: "mesa.modeConfirm", n: counts.confirm },
    { key: "prohibited", labelKey: "mesa.modeProhibited", n: counts.prohibited },
    { key: "read_only", labelKey: "mesa.modeReadOnly", n: counts.read_only },
    { key: "enforced", labelKey: "mesa.modeEnforced", n: counts.enforced },
    { key: "inherited", labelKey: "mesa.modeInherited", n: counts.inherited },
    { key: "autonomous", labelKey: "mesa.modeAutonomous", n: counts.autonomous },
  ].filter((c) => c.n > 0);

  const totalCount = profiles.length + scopeDefs.reduce((n, d) => n + d.rows.length, 0);

  return (
    <div className="view-root">
      <div className="filter-row" style={{ justifyContent: "space-between" }}>
        {/* Ordered by inheritance specificity (entity > device > area >
            integration > domain, i.e. mesa_core's SCOPE_RANK), so the toolbar
            reads as the cascade the whole tab is organised around. Entity leads
            because it is the most specific level AND the most common action;
            it previously sat last, where being the primary-styled button put
            the most specific scope after the least specific one. */}
        <div className="filter-row-right mesa-toolbar-add">
          <button className="btn btn-primary btn-sm" onClick={() => setEditing({ scope: "entity", key: null, isNew: true })}>
            <span className="btn-label-full">{t("mesa.addEntityProfile")}</span>
            <span className="btn-label-short">{t("mesa.addEntityShort")}</span>
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setEditing({ scope: "device", key: null, isNew: true })}>
            <span className="btn-label-full">{t("mesa.addDeviceProfile")}</span>
            <span className="btn-label-short">{t("mesa.addDeviceShort")}</span>
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setEditing({ scope: "area", key: null, isNew: true })}>
            <span className="btn-label-full">{t("mesa.addAreaProfile")}</span>
            <span className="btn-label-short">{t("mesa.addAreaShort")}</span>
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setEditing({ scope: "integration", key: null, isNew: true })}>
            <span className="btn-label-full">{t("mesa.addIntegrationProfile")}</span>
            <span className="btn-label-short">{t("mesa.addIntegrationShort")}</span>
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setEditing({ scope: "domain", key: null, isNew: true })}>
            <span className="btn-label-full">{t("mesa.addDomainProfile")}</span>
            <span className="btn-label-short">{t("mesa.addDomainShort")}</span>
          </button>
        </div>
        <div className="filter-row-right mesa-toolbar-tools">
          <button className="btn btn-ghost btn-sm btn-icon" onClick={() => setShowImport(true)} aria-label={t("mesa.importTitle")} title={t("mesa.importTitle")}><ImportIcon /></button>
          <button className="btn btn-ghost btn-sm btn-icon" onClick={() => setShowExport(true)} aria-label={t("mesa.exportTitle")} title={t("mesa.exportTitle")}><ExportIcon /></button>
          <button className="btn btn-ghost btn-sm btn-icon" onClick={refresh} aria-label={t("common.refresh")} title={t("common.refresh")}><RefreshIcon /></button>
        </div>
      </div>

      {error && <ErrorMsg msg={error} />}

      {(issues.issues.length > 0 || issues.orphans.length > 0 || issues.orphan_devices.length > 0 || issues.orphan_areas.length > 0 || issues.orphan_integrations.length > 0) && (
        <div className="banner banner-warn">
          {issues.issues.length > 0 && (
            <div>
              <strong>{t("mesa.triggerIssues", { count: issues.issues.length })}</strong>
              <ul>
                {issues.issues.map((i, idx) => (
                  <li key={idx}>{tRich("mesa.triggerIssueRow", { code: (c) => <code>{c}</code> }, { entityId: i.entity_id, declared: i.declared_value, automationId: i.automation_id, role: i.role })}</li>
                ))}
              </ul>
            </div>
          )}
          {issues.orphans.length > 0 && (
            <div>
              {tRich("mesa.orphanEntities", { strong: (c) => <strong>{c}</strong> }, { count: issues.orphans.length, list: issues.orphans.join(t("common.listSeparator")) })}
            </div>
          )}
          {issues.orphan_devices.length > 0 && (
            <p className="mesa-orphan-line">
              {tRich("mesa.orphanDevices", { strong: (c) => <strong>{c}</strong> }, { count: issues.orphan_devices.length, list: issues.orphan_devices.join(t("common.listSeparator")) })}
            </p>
          )}
          {issues.orphan_areas.length > 0 && (
            <div>
              {tRich("mesa.orphanAreas", { strong: (c) => <strong>{c}</strong> }, { count: issues.orphan_areas.length, list: issues.orphan_areas.join(t("common.listSeparator")) })}
            </div>
          )}
          {issues.orphan_integrations.length > 0 && (
            <div>
              {tRich("mesa.orphanIntegrations", { strong: (c) => <strong>{c}</strong> }, { count: issues.orphan_integrations.length, list: issues.orphan_integrations.join(t("common.listSeparator")) })}
            </div>
          )}
          {(issues.orphans.length > 0 || issues.orphan_devices.length > 0 || issues.orphan_areas.length > 0 || issues.orphan_integrations.length > 0) && (
            <div style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "10px" }}>
              {confirmClearOrphans ? (
                <>
                  <span>{t("mesa.deleteAllOrphans", { count: issues.orphans.length + issues.orphan_devices.length + issues.orphan_areas.length + issues.orphan_integrations.length })}</span>
                  <button className="btn btn-danger btn-sm" onClick={clearOrphans} disabled={clearingOrphans}>
                    {clearingOrphans ? t("mesa.clearing") : t("mesa.yesDelete")}
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setConfirmClearOrphans(false)} disabled={clearingOrphans}>{t("mesa.cancel")}</button>
                </>
              ) : (
                <button className="btn btn-sm" onClick={() => setConfirmClearOrphans(true)}>{t("mesa.clearAllOrphans")}</button>
              )}
            </div>
          )}
        </div>
      )}

      <MesaSuggestions
        suggestions={issues.suggestions}
        dismissedCount={issues.dismissed_suggestions.length}
        busyKey={suggestBusyKey}
        rescanning={rescanning}
        onApply={applySuggestion}
        onReview={(s) => setEditing({
          scope: s.scope === "domain" ? "domain" : "entity",
          key: s.subject_id,
          isNew: true,
          seedControlMode: s.suggested_mode,
          // The suggestion's subject comes from a live backend scan (hass.states
          // / mesa-core resolution), which can be ahead of the frontend's
          // once-per-mount entityTree snapshot. Lock the key so the editor
          // trusts the supplied id instead of validating it against that
          // possibly-stale local cache (same mechanism the in-context
          // injector uses for unregistered entities).
          lockedKey: true,
        })}
        onDismiss={dismissSuggestion}
        onRestoreAll={restoreAllSuggestions}
        onRescan={rescanSuggestions}
      />

      {totalCount > 0 && (
        <div className="mesa-controls">
          <div className="mesa-summary" role="group" aria-label={t("mesa.filterByControlMode")}>
            <button className={`mesa-chip${filter === "" ? " mesa-chip-active" : ""}`} aria-pressed={filter === ""} onClick={() => setFilter("")}>
              <span className="sr-only">{filter === "" ? t("mesa.currentFilter") : ""}</span>
              {t("mesa.filterAll")} <span className="mesa-chip-count">{totalCount}</span>
            </button>
            {chips.map((c) => (
              <button key={c.key}
                className={`mesa-chip${filter === c.key ? " mesa-chip-active" : ""}`}
                aria-pressed={filter === c.key}
                onClick={() => setFilter(filter === c.key ? "" : c.key)}>
                {t(c.labelKey)} <span className="mesa-chip-count">{c.n}</span>
              </button>
            ))}
          </div>
          <input className="input mesa-search" placeholder={t("mesa.searchPlaceholder")}
            value={search} onChange={(e) => setSearch(e.target.value)} aria-label={t("mesa.searchAria")} />
        </div>
      )}

      {loading ? <Loading /> : totalCount === 0 ? (
        <div className="card">
          <p className="token-table-empty">{t("mesa.emptyState")}</p>
        </div>
      ) : (filtered.length === 0 && scopeFiltered.length === 0) ? (
        <div className="card"><p className="token-table-empty">{t("mesa.noMatch")}</p></div>
      ) : (
        <>
          {scopeFiltered.length > 0 && (
            <>
              <p className="mesa-scope-note">
                {tRich("mesa.scopeNote", { badge: (c) => <span className="badge badge-purple">{c}</span> })}
              </p>
              <div className="mesa-groups">
                {scopeFiltered.map((c) => {
                  const isCollapsed = collapsed.has(c.sentinel);
                  return (
                    <div key={c.sentinel} className="card mesa-group">
                      <button className="mesa-group-header" onClick={() => toggleGroup(c.sentinel)} aria-expanded={!isCollapsed}>
                        <span className={`collapsible-chevron${!isCollapsed ? " open" : ""}`} aria-hidden="true" />
                        <span className="mesa-group-scope">{t(c.titleKey)}</span>
                        <span className="mesa-group-count">{c.rows.length}</span>
                      </button>
                      {!isCollapsed && (
                        <table className="data-table mesa-profile-table">
                          <tbody>
                            {c.rows.map((r) => {
                              const name = scopeKeyName(c.scope as CascadingScope, r.key);
                              return (
                              <tr key={r.key}>
                                <td>
                                  <button
                                    type="button"
                                    className="row-link-btn"
                                    onClick={() => setEditing({ scope: c.scope, key: r.key, isNew: false })}
                                  >
                                    {/* Name first, key second, mirroring the entity rows. The key
                                        is still shown because it is what the API and the audit log
                                        record, and for an orphaned profile it is all that is left. */}
                                    <span className="mesa-row-name">{name}</span>
                                    {name !== r.key && <code className="mesa-row-id">{r.key}</code>}
                                  </button>
                                </td>
                                <td className="mesa-row-modes">
                                  {c.scope === "integration" && profileSource(r.document) === "developer" && (
                                    <span className="badge badge-purple" title={t("mesa.vendorTitle")}>{t("mesa.vendorBadge")}</span>
                                  )}
                                  <ControlBadge mode={rawControlMode(r.document)} />
                                  {isEnforced(r.document) && <span className="badge badge-blue">{t("mesa.modeEnforced")}</span>}
                                </td>
                                <td className="mesa-row-tags">{tagsOf(r.document).join(t("common.listSeparator"))}</td>
                              </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {filtered.length > 0 && (
            <div className="mesa-groups">
              {groups.map(([domain, items]) => {
                const isCollapsed = collapsed.has(domain);
                return (
                  <div key={domain} className="card mesa-group">
                    <button className="mesa-group-header" onClick={() => toggleGroup(domain)} aria-expanded={!isCollapsed}>
                      <span className={`collapsible-chevron${!isCollapsed ? " open" : ""}`} aria-hidden="true" />
                      <code>{domain}</code>
                      <span className="mesa-group-count">{items.length}</span>
                    </button>
                    {!isCollapsed && (
                      <table className="data-table mesa-profile-table">
                        <tbody>
                          {items.map((p) => (
                            <tr key={p.entity_id}>
                              <td>
                                <button
                                  type="button"
                                  className="row-link-btn row-link-btn-stack"
                                  onClick={() => setEditing({ scope: "entity", key: p.entity_id, isNew: false })}
                                >
                                  <span className="mesa-row-name">{friendly(p.entity_id) || p.entity_id}</span>
                                  {friendly(p.entity_id) && <code className="mesa-row-id">{p.entity_id}</code>}
                                </button>
                              </td>
                              <td className="mesa-row-modes">
                                <ControlBadge mode={rawControlMode(p.document)} />
                                {isEnforced(p.document) && <span className="badge badge-blue">{t("mesa.modeEnforced")}</span>}
                              </td>
                              <td className="mesa-row-tags">{tagsOf(p.document).join(t("common.listSeparator"))}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {editing && (
        <ProfileEditor
          scope={editing.scope}
          profileKey={editing.key}
          isNew={editing.isNew}
          entityTree={entityTree}
          canonicalTags={canonicalTags}
          integrationOptions={integrationOptions}
          deviceOptions={deviceOptions}
          onClose={() => setEditing(null)}
          onSaved={refresh}
          seedControlMode={editing.seedControlMode}
          lockedKey={editing.lockedKey}
          keyLabel={
            editing.key && editing.scope !== "entity" && editing.scope !== "domain"
              ? scopeKeyName(editing.scope as CascadingScope, editing.key)
              : undefined
          }
        />
      )}

      {showExport && <ExportModal profileCount={totalCount} onClose={() => setShowExport(false)} />}
      {showImport && (
        <ImportModal
          onClose={(didImport) => {
            setShowImport(false);
            if (didImport) refresh();
          }}
        />
      )}
    </div>
  );
}
