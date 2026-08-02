import { useState, useMemo } from "react";
import type { EntityTree, NodeState } from "../types";
import { api } from "../api";
import { Modal } from "./Modal";
import { compareStrings, t, tn } from "../i18n";

interface Props {
  tokenId: string;
  entityTree: EntityTree;
  onDone: () => void;
  onClose: () => void;
}

type Mode = "area" | "label";

const STATES: { state: NodeState; labelKey: string }[] = [
  { state: "YELLOW", labelKey: "selectBy.stateRead" },
  { state: "GREEN", labelKey: "selectBy.stateWrite" },
  { state: "RED", labelKey: "selectBy.stateDeny" },
  { state: "GREY", labelKey: "selectBy.stateRemoveGrant" },
];

// The summary sentence names the permission the same way the dropdown above it
// does. Interpolating the raw NodeState instead put "GREEN" in a sentence whose
// own dropdown said "Write", and left the tree's node states untranslated in
// every locale.
const STATE_LABEL_KEYS = Object.fromEntries(
  STATES.map((s) => [s.state, s.labelKey]),
) as Record<NodeState, string>;

export function SelectByPicker({ tokenId, entityTree, onDone, onClose }: Props) {
  const [mode, setMode] = useState<Mode>("area");
  const [selectedKey, setSelectedKey] = useState<string>("");
  const [selectedState, setSelectedState] = useState<NodeState>("GREEN");
  const [applying, setApplying] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Groups of (id, name, entity count) for the current mode, sorted by name.
  const groups = useMemo(() => {
    const map = new Map<string, { id: string; name: string; count: number }>();
    for (const domain of Object.values(entityTree)) {
      for (const detail of Object.values(domain.entity_details)) {
        if (mode === "area") {
          if (detail.area_id && detail.area_name) {
            const existing = map.get(detail.area_id);
            if (existing) existing.count++;
            else map.set(detail.area_id, { id: detail.area_id, name: detail.area_name, count: 1 });
          }
        } else {
          for (const label of detail.labels) {
            const existing = map.get(label.id);
            if (existing) existing.count++;
            else map.set(label.id, { id: label.id, name: label.name, count: 1 });
          }
        }
      }
    }
    return Array.from(map.values()).sort((a, b) => compareStrings(a.name, b.name));
  }, [entityTree, mode]);

  const affectedEntities = useMemo(() => {
    if (!selectedKey) return [];
    const result: string[] = [];
    for (const domain of Object.values(entityTree)) {
      for (const detail of Object.values(domain.entity_details)) {
        const inGroup =
          mode === "area"
            ? detail.area_id === selectedKey
            : detail.labels.some((l) => l.id === selectedKey);
        if (inGroup) result.push(detail.entity_id);
      }
    }
    return result;
  }, [selectedKey, entityTree, mode]);

  function switchMode(next: Mode) {
    if (next === mode) return;
    setMode(next);
    setSelectedKey("");
  }

  async function apply() {
    if (!selectedKey || affectedEntities.length === 0) return;
    setApplying(true);
    setError(null);
    let done = 0;
    try {
      for (const entityId of affectedEntities) {
        setProgress(`${done + 1} / ${affectedEntities.length}`);
        await api.patchEntityPermission(tokenId, entityId, { state: selectedState });
        done++;
      }
      onDone();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("selectBy.applyFailed"));
    } finally {
      setApplying(false);
      setProgress(null);
    }
  }

  return (
    <Modal titleId="area-picker-title" onClose={applying ? undefined : onClose}>
      <h3 className="modal-title" id="area-picker-title">{t("selectBy.title")}</h3>

      <div className="wizard-tabs" role="group" aria-label={t("selectBy.groupBy")}>
        {(["area", "label"] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            className={`wizard-tab${mode === m ? " wizard-tab-active" : ""}`}
            onClick={() => switchMode(m)}
            disabled={applying}
          >
            {m === "area" ? t("selectBy.area") : t("selectBy.label")}
          </button>
        ))}
      </div>

      <div className="banner banner-warn">
          {mode === "area" ? t("selectBy.grantNoteArea") : t("selectBy.grantNoteLabel")}
        </div>

        <div className="field">
          <label htmlFor="select-by-key">{mode === "area" ? t("selectBy.area") : t("selectBy.label")}</label>
          <select
            id="select-by-key"
            className="input"
            value={selectedKey}
            onChange={(e) => setSelectedKey(e.target.value)}
          >
            <option value="">{mode === "area" ? t("selectBy.selectArea") : t("selectBy.selectLabel")}</option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name} ({tn("selectBy.entities", g.count)})
              </option>
            ))}
          </select>
          {groups.length === 0 && (
            <p className="area-picker-summary">{mode === "area" ? t("selectBy.noAreas") : t("selectBy.noLabels")}</p>
          )}
        </div>

        <div className="field">
          <label htmlFor="select-by-permission">{t("selectBy.permissionToApply")}</label>
          <select
            id="select-by-permission"
            className="input"
            value={selectedState}
            onChange={(e) => setSelectedState(e.target.value as NodeState)}
          >
            {STATES.map((s) => (
              <option key={s.state} value={s.state}>{t(s.labelKey)}</option>
            ))}
          </select>
        </div>

        {selectedKey && (
          <p className="area-picker-summary">
            {tn("selectBy.willSet", affectedEntities.length, { state: t(STATE_LABEL_KEYS[selectedState]) })}
          </p>
        )}

        {error && <div className="banner banner-error" role="alert">{error}</div>}
        {progress && <p className="area-picker-progress" role="status">{t("selectBy.applyingProgress", { progress })}</p>}

      <div className="modal-actions">
        <button
          className="btn btn-primary"
          onClick={apply}
          disabled={applying || !selectedKey || affectedEntities.length === 0}
        >
          {applying ? t("selectBy.applying") : t("selectBy.apply")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={applying}>{t("selectBy.cancel")}</button>
      </div>
    </Modal>
  );
}
