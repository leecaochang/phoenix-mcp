import { useEffect, useMemo, useState } from "react";
import type {
  EntityTree,
  IntegrationPermissionOption,
  NodeState,
} from "../types";
import { api } from "../api";
import { Modal } from "./Modal";
import { compareStrings, t, tn } from "../i18n";

interface Props {
  tokenId: string;
  entityTree: EntityTree;
  onDone: () => void;
  onClose: () => void;
}

type Mode = "area" | "label" | "integration";

const STATES: { state: NodeState; labelKey: string }[] = [
  { state: "YELLOW", labelKey: "selectBy.stateRead" },
  { state: "GREEN", labelKey: "selectBy.stateWrite" },
  { state: "RED", labelKey: "selectBy.stateDeny" },
  { state: "GREY", labelKey: "selectBy.stateRemoveGrant" },
];

const STATE_LABEL_KEYS = Object.fromEntries(
  STATES.map((item) => [item.state, item.labelKey]),
) as Record<NodeState, string>;

interface Group {
  id: string;
  name: string;
  count: number;
  integration?: IntegrationPermissionOption;
}

export function SelectByPicker({ tokenId, entityTree, onDone, onClose }: Props) {
  const [mode, setMode] = useState<Mode>("area");
  const [selectedKey, setSelectedKey] = useState("");
  const [selectedState, setSelectedState] = useState<NodeState>("GREEN");
  const [integrations, setIntegrations] = useState<IntegrationPermissionOption[]>([]);
  const [loadingIntegrations, setLoadingIntegrations] = useState(true);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.getPermissionIntegrationOptions(tokenId)
      .then((result) => {
        if (active) setIntegrations(result.integrations);
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : t("selectBy.loadIntegrationsFailed"));
        }
      })
      .finally(() => {
        if (active) setLoadingIntegrations(false);
      });
    return () => { active = false; };
  }, [tokenId]);

  const groups = useMemo<Group[]>(() => {
    if (mode === "integration") {
      return integrations.map((integration) => ({
        id: integration.entry_id,
        name: `${integration.title} — ${integration.domain}`,
        count: integration.device_count + integration.deviceless_entity_count,
        integration,
      })).sort((a, b) => compareStrings(a.name, b.name));
    }
    const map = new Map<string, Group>();
    for (const domain of Object.values(entityTree)) {
      for (const detail of Object.values(domain.entity_details)) {
        if (mode === "area") {
          if (detail.area_id && detail.area_name) {
            const existing = map.get(detail.area_id);
            if (existing) existing.count++;
            else map.set(detail.area_id, {
              id: detail.area_id, name: detail.area_name, count: 1,
            });
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
  }, [entityTree, integrations, mode]);

  const selectedGroup = groups.find((group) => group.id === selectedKey);
  const selectedIntegration = selectedGroup?.integration;
  const affectedCount = selectedGroup?.count ?? 0;

  function switchMode(next: Mode) {
    if (next === mode) return;
    setMode(next);
    setSelectedKey("");
    setError(null);
  }

  async function apply() {
    if (!selectedKey || affectedCount === 0) return;
    setApplying(true);
    setError(null);
    try {
      await api.bulkSelectPermissions(tokenId, mode, selectedKey, selectedState);
      onDone();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : t("selectBy.applyFailed"));
    } finally {
      setApplying(false);
    }
  }

  const selectLabel = mode === "area"
    ? t("selectBy.area")
    : mode === "label"
      ? t("selectBy.label")
      : t("selectBy.integration");
  const placeholder = mode === "area"
    ? t("selectBy.selectArea")
    : mode === "label"
      ? t("selectBy.selectLabel")
      : t("selectBy.selectIntegration");

  return (
    <Modal titleId="permission-picker-title" onClose={applying ? undefined : onClose}>
      <h3 className="modal-title" id="permission-picker-title">{t("selectBy.title")}</h3>

      <div className="wizard-tabs" role="group" aria-label={t("selectBy.groupBy")}>
        {(["area", "label", "integration"] as Mode[]).map((item) => (
          <button
            key={item}
            type="button"
            aria-pressed={mode === item}
            className={`wizard-tab${mode === item ? " wizard-tab-active" : ""}`}
            onClick={() => switchMode(item)}
            disabled={applying}
          >
            {item === "area" ? t("selectBy.area") : item === "label" ? t("selectBy.label") : t("selectBy.integration")}
          </button>
        ))}
      </div>

      <div className="banner banner-warn">
        {mode === "area"
          ? t("selectBy.grantNoteArea")
          : mode === "label"
            ? t("selectBy.grantNoteLabel")
            : t("selectBy.grantNoteIntegration")}
      </div>

      <div className="field">
        <label htmlFor="select-by-key">{selectLabel}</label>
        <select
          id="select-by-key"
          className="input"
          value={selectedKey}
          onChange={(event) => setSelectedKey(event.target.value)}
          disabled={applying || (mode === "integration" && loadingIntegrations)}
        >
          <option value="">{loadingIntegrations && mode === "integration" ? t("selectBy.loadingIntegrations") : placeholder}</option>
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {group.name} ({mode === "integration" ? tn("selectBy.resources", group.count) : tn("selectBy.entities", group.count)})
            </option>
          ))}
        </select>
        {!loadingIntegrations && groups.length === 0 && (
          <p className="area-picker-summary">
            {mode === "area" ? t("selectBy.noAreas") : mode === "label" ? t("selectBy.noLabels") : t("selectBy.noIntegrations")}
          </p>
        )}
      </div>

      {selectedIntegration && (
        <div className="integration-selection-details">
          <span>{t("selectBy.entryId")}</span>
          <code className="integration-entry-id">{selectedIntegration.entry_id}</code>
          <p>{t("selectBy.integrationCoverage", {
            devices: selectedIntegration.device_count,
            entities: selectedIntegration.deviceless_entity_count,
          })}</p>
          {selectedIntegration.registry_only_deviceless_count > 0 && (
            <div className="banner banner-warn">
              {t("selectBy.registryOnlyWarning", {
                count: selectedIntegration.registry_only_deviceless_count,
                domains: selectedIntegration.required_domain_ids.join(", "),
              })}
            </div>
          )}
          {selectedIntegration.shared_device_count > 0 && (
            <div className="banner banner-warn">
              {tn("selectBy.sharedDeviceWarning", selectedIntegration.shared_device_count)}
            </div>
          )}
        </div>
      )}

      <div className="field">
        <label htmlFor="select-by-permission">{t("selectBy.permissionToApply")}</label>
        <select
          id="select-by-permission"
          className="input"
          value={selectedState}
          onChange={(event) => setSelectedState(event.target.value as NodeState)}
          disabled={applying}
        >
          {STATES.map((item) => (
            <option key={item.state} value={item.state}>{t(item.labelKey)}</option>
          ))}
        </select>
      </div>

      {selectedKey && (
        <p className="area-picker-summary">
          {mode === "integration"
            ? t("selectBy.willSetIntegration", {
              state: t(STATE_LABEL_KEYS[selectedState]),
              devices: selectedIntegration?.device_count ?? 0,
              entities: selectedIntegration?.deviceless_entity_count ?? 0,
            })
            : tn("selectBy.willSet", affectedCount, { state: t(STATE_LABEL_KEYS[selectedState]) })}
        </p>
      )}

      {error && <div className="banner banner-error" role="alert">{error}</div>}

      <div className="modal-actions">
        <button
          className="btn btn-primary"
          onClick={apply}
          disabled={applying || !selectedKey || affectedCount === 0}
        >
          {applying ? t("selectBy.applying") : t("selectBy.apply")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={applying}>{t("selectBy.cancel")}</button>
      </div>
    </Modal>
  );
}
