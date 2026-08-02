import type { GlobalSettings } from "../types";
import { t } from "../i18n";

interface Props {
  settings: GlobalSettings;
  onToggle: (value: boolean) => void;
  saving: boolean;
}

export function KillSwitch({ settings, onToggle, saving }: Props) {
  const active = settings.kill_switch;

  return (
    <div className={active ? "kill-switch-active" : ""}>
      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span className={active ? "kill-switch-label-active" : "kill-switch-label"}>
            {active ? t("settings.killSwitchActiveLabel") : t("settings.killSwitchDisableLabel")}
          </span>
          <small>
            {active
              ? t("settings.killSwitchActiveHelp")
              : t("settings.killSwitchInactiveHelp")}
          </small>
        </div>
        <label className={`toggle-switch toggle-switch-danger${saving ? " disabled" : ""}`}>
          <input
            type="checkbox"
            aria-label={active ? t("settings.killSwitchActiveAria") : t("settings.killSwitchDisableLabel")}
            checked={active}
            disabled={saving}
            onChange={(e) => onToggle(e.target.checked)}
          />
          <span className="toggle-switch-track" />
        </label>
      </div>
    </div>
  );
}
