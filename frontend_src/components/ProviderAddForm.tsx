import { useEffect, useRef, useState } from "react";
import { api, localizedApiMessage } from "../api";
import OPENCODE_ICON from "../assets/opencode.svg";
import { t } from "../i18n";
import type {
  AgentCliInstance,
  AgentCliProviderField,
  AgentCliProviderKind,
  AgentCliProviderType,
} from "../types";

interface FormState {
  values: Record<string, string>;
  model: string;
  models: string[];
  validating: boolean;
  validated: boolean;
  saving: boolean;
  error: string | null;
  probe: boolean;
}

const EMPTY_FORM: FormState = {
  values: {}, model: "", models: [], validating: false, validated: false,
  saving: false, error: null, probe: true,
};

export interface ProviderAddFormProps {
  providerTypes: AgentCliProviderType[];
  disabled?: boolean;
  completeLabel: string;
  addingLabelKey: string;
  onActiveChange?: (active: boolean) => void;
  onCreated: (instance: AgentCliInstance, probe: boolean) => Promise<void> | void;
}

function providerLabel(provider: AgentCliProviderType): string {
  return provider.label_key ? t(provider.label_key) : provider.label;
}

function fieldLabel(field: AgentCliProviderField): string {
  return t(field.label_key);
}

function initialValues(provider: AgentCliProviderType): Record<string, string> {
  return Object.fromEntries(provider.fields.map((field) => [
    field.id,
    field.type === "choice" ? field.choices?.[0]?.value ?? "" : "",
  ]));
}

export function ProviderAddForm({
  providerTypes, disabled, completeLabel, addingLabelKey, onActiveChange, onCreated,
}: ProviderAddFormProps) {
  const [pickerValue, setPickerValue] = useState("");
  const [adding, setAdding] = useState<AgentCliProviderType | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const pickerRef = useRef<HTMLSelectElement>(null);
  const firstFieldRef = useRef<HTMLInputElement | HTMLSelectElement>(null);

  useEffect(() => { onActiveChange?.(adding !== null); }, [adding, onActiveChange]);
  useEffect(() => {
    if (adding) window.requestAnimationFrame(() => firstFieldRef.current?.focus());
  }, [adding]);

  const selectProvider = (kind: string) => {
    setPickerValue(kind);
    const provider = providerTypes.find((item) => item.kind === kind) ?? null;
    setAdding(provider);
    setForm(provider ? { ...EMPTY_FORM, values: initialValues(provider) } : EMPTY_FORM);
  };

  const reset = (focus = true) => {
    setPickerValue("");
    setAdding(null);
    setForm(EMPTY_FORM);
    if (focus) window.requestAnimationFrame(() => pickerRef.current?.focus());
  };

  const setValue = (id: string, value: string) => {
    setForm((current) => ({
      ...current,
      values: { ...current.values, [id]: value },
      validated: false,
      error: null,
    }));
  };

  const validate = async () => {
    if (!adding) return;
    // The picker is a launch control, not persistent form state. Reset it on
    // every validation attempt while the open form retains the selected type.
    setPickerValue("");
    const missing = adding.fields.find(
      (field) => field.required && !(form.values[field.id] ?? "").trim(),
    );
    if (missing) {
      setForm((current) => ({
        ...current,
        error: t("settings.agentcliFieldRequired", { field: fieldLabel(missing) }),
      }));
      return;
    }
    setForm((current) => ({ ...current, validating: true, error: null }));
    try {
      const payload = Object.fromEntries(
        Object.entries(form.values).map(([key, value]) => [key, value.trim()]),
      );
      const response = await api.probeAgentCliProvider(
        adding.kind as AgentCliProviderKind,
        payload,
      );
      if (!response || !response.ok || !Array.isArray(response.models)) {
        const error = response?.error
          ? localizedApiMessage(response.error, response.message_key, response.message_params)
          : t("settings.agentcliConnectionFailed");
        setForm((current) => ({
          ...current, validating: false, validated: false, models: [], error,
        }));
        return;
      }
      setForm((current) => ({
        ...current,
        validating: false,
        validated: true,
        error: null,
        models: response.models,
        model: response.models.includes(current.model)
          ? current.model : response.models[0] ?? "",
      }));
    } catch (error: unknown) {
      setForm((current) => ({
        ...current,
        validating: false,
        validated: false,
        models: [],
        error: error instanceof Error ? error.message : t("settings.agentcliConnectionFailed"),
      }));
    }
  };

  const complete = async () => {
    if (!adding) return;
    setForm((current) => ({ ...current, saving: true, error: null }));
    try {
      const payload = {
        ...Object.fromEntries(
          Object.entries(form.values).map(([key, value]) => [key, value.trim()]),
        ),
        ...(form.model ? { model: form.model } : {}),
      };
      const response = await api.createAgentCliProvider(adding.kind, payload);
      await onCreated(response.instance, form.probe && Boolean(form.model));
      reset(false);
    } catch (error: unknown) {
      setForm((current) => ({
        ...current,
        saving: false,
        error: error instanceof Error ? error.message : t("settings.agentcliSaveFailed"),
      }));
    }
  };

  const controlsDisabled = Boolean(disabled || adding || form.saving);

  return (
    <>
      <div className="agentcli-add-row">
        <select
          ref={pickerRef}
          className="input input-auto"
          value={pickerValue}
          disabled={controlsDisabled || providerTypes.length === 0}
          onChange={(event) => selectProvider(event.target.value)}
          aria-label={t("settings.agentcliProviderType")}
        >
          <option value="" disabled>{t("settings.agentcliAddProvider")}</option>
          {providerTypes.map((provider) => (
            <option key={provider.kind} value={provider.kind}>{providerLabel(provider)}</option>
          ))}
        </select>
      </div>

      {adding && (
        <div className="agentcli-settings-form agentcli-add-form">
          {adding.kind === "opencode" && (
            <img className="agentcli-provider-logo" src={OPENCODE_ICON} alt="" aria-hidden="true" />
          )}
          <div className="agentcli-settings-hint">
            {t(addingLabelKey, { name: providerLabel(adding), label: providerLabel(adding) })}
          </div>
          {adding.fields.map((field, index) => {
            const id = `agentcli-add-${adding.kind}-${field.id}`;
            return (
              <label className="agentcli-settings-field" htmlFor={id} key={field.id}>
                <span>{fieldLabel(field)}</span>
                {field.type === "choice" ? (
                  <select
                    id={id}
                    ref={index === 0 ? (element) => { firstFieldRef.current = element; } : undefined}
                    value={form.values[field.id] ?? ""}
                    disabled={form.validating || form.saving}
                    aria-describedby={form.error ? "agentcli-add-error" : undefined}
                    aria-invalid={form.error ? true : undefined}
                    onChange={(event) => setValue(field.id, event.target.value)}
                  >
                    {(field.choices ?? []).map((choice) => (
                      <option value={choice.value} key={choice.value}>
                        {choice.label_key ? t(choice.label_key) : choice.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    id={id}
                    ref={index === 0 ? (element) => { firstFieldRef.current = element; } : undefined}
                    type={field.type === "secret" ? "password" : "url"}
                    placeholder={field.placeholder ?? fieldLabel(field)}
                    value={form.values[field.id] ?? ""}
                    disabled={form.validating || form.saving}
                    aria-describedby={form.error ? "agentcli-add-error" : undefined}
                    aria-invalid={form.error ? true : undefined}
                    onChange={(event) => setValue(field.id, event.target.value)}
                  />
                )}
              </label>
            );
          })}
          {form.validating && (
            <div className="agentcli-settings-hint" role="status">
              {t("settings.agentcliValidating")}
            </div>
          )}
          <span className="sr-only" role="status" aria-live="polite">
            {form.validated ? t("settings.agentcliValidated") : ""}
          </span>
          {form.error && (
            <div id="agentcli-add-error" className="banner banner-error" role="alert">
              {form.error}
            </div>
          )}
          {form.validated && (
            <label className="agentcli-settings-model-row">
              <span>{t("settings.agentcliSelectModel")}</span>
              <select
                value={form.model}
                disabled={form.saving || !form.models.length}
                onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
              >
                {form.models.length
                  ? form.models.map((model) => <option key={model} value={model}>{model}</option>)
                  : <option value="">{t("settings.agentcliNoModels")}</option>}
              </select>
            </label>
          )}
          {form.validated && (
            <label className="agentcli-settings-probe-opt">
              <input
                type="checkbox"
                checked={form.probe}
                onChange={(event) => setForm((current) => ({ ...current, probe: event.target.checked }))}
              />
              <span>{t("settings.agentcliProbeOnAdd")}</span>
            </label>
          )}
          <div className="agentcli-settings-form-actions">
            {form.validated ? (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={form.saving}
                onClick={() => void complete()}
              >
                {completeLabel}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={form.validating || form.saving}
                onClick={() => void validate()}
              >
                {t("settings.agentcliValidate")}
              </button>
            )}
            <button
              type="button"
              className="btn btn-sm"
              disabled={form.saving}
              onClick={() => reset()}
            >
              {t("settings.cancel")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
