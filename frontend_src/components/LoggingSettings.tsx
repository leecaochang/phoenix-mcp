import type { GlobalSettings } from "../types";
import { t } from "../i18n";

interface Props {
  settings: GlobalSettings;
  onToggle: (key: keyof GlobalSettings, value: boolean) => void;
  saving: boolean;
}

const TOGGLES: {
  key: keyof Pick<
    GlobalSettings,
    "disable_all_logging" | "log_allowed" | "log_denied" | "log_rate_limited" | "log_entity_names" | "log_client_ip"
  >;
  labelKey: string;
  descriptionKey: string;
  master?: boolean;
}[] = [
  {
    key: "disable_all_logging",
    labelKey: "settings.logDisableAllLabel",
    descriptionKey: "settings.logDisableAllHelp",
    master: true,
  },
  {
    key: "log_allowed",
    labelKey: "settings.logAllowedLabel",
    descriptionKey: "settings.logAllowedHelp",
  },
  {
    key: "log_denied",
    labelKey: "settings.logDeniedLabel",
    descriptionKey: "settings.logDeniedHelp",
  },
  {
    key: "log_rate_limited",
    labelKey: "settings.logRateLimitedLabel",
    descriptionKey: "settings.logRateLimitedHelp",
  },
  {
    key: "log_entity_names",
    labelKey: "settings.logEntityIdsLabel",
    descriptionKey: "settings.logEntityIdsHelp",
  },
  {
    key: "log_client_ip",
    labelKey: "settings.logClientIpLabel",
    descriptionKey: "settings.logClientIpHelp",
  },
];

export function LoggingSettings({ settings, onToggle, saving }: Props) {
  const masterOff = settings.disable_all_logging;

  return (
    <div>
      {TOGGLES.map(({ key, labelKey, descriptionKey, master }) => {
        const greyed = !master && masterOff;
        return (
          <div
            key={key}
            className={`toggle-row${greyed ? " toggle-row-greyed" : ""}`}
          >
            <div className="toggle-label">
              <span className={master ? "toggle-label-master" : undefined}>{t(labelKey)}</span>
              <small>{t(descriptionKey)}</small>
            </div>
            <label className={`toggle-switch${(saving || greyed) ? " disabled" : ""}`}>
              <input
                type="checkbox"
                aria-label={t(labelKey)}
                checked={settings[key] as boolean}
                disabled={saving || greyed}
                onChange={(e) => onToggle(key, e.target.checked)}
              />
              <span className="toggle-switch-track" />
            </label>
          </div>
        );
      })}
    </div>
  );
}
