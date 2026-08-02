import { useEffect, useState } from "react";
import type { GlobalSettings } from "../types";
import { api } from "../api";
import { LoggingSettings } from "../components/LoggingSettings";
import { NotificationSettings } from "../components/NotificationSettings";
import { KillSwitch } from "../components/KillSwitch";
import { AgentCliSettings } from "../components/AgentCliSettings";
import { AssistBridgeSettings } from "../components/AssistBridgeSettings";
import { VoiceAgentSettings } from "../components/VoiceAgentSettings";
import { AiTaskSettings } from "../components/AiTaskSettings";
import { WipeConfirmModal } from "../components/WipeConfirmModal";
import { DocsHelpLink } from "../components/common";
import { Loading } from "../index";
import { JS_BUILD } from "../version";
import { LANGUAGES, LANGUAGE_AUTO, t } from "../i18n";
import { tRich } from "../i18n/rich";

type Theme = "light" | "dark" | "auto";

// The theme values stay lowercase internally (they are the stored setting), so
// the button captions are a lookup rather than a capitalization of the value.
const THEME_LABEL_KEYS: Record<Theme, string> = {
  light: "settings.themeLight",
  auto: "settings.themeAuto",
  dark: "settings.themeDark",
};

interface Props {
  settings: GlobalSettings | null;
  onSettingsChange: (s: GlobalSettings) => void;
  theme: Theme;
  onThemeChange: (t: Theme) => void;
  language: string;
  onLanguageChange: (lang: string) => void;
}

export function SettingsView({ settings, onSettingsChange, theme, onThemeChange, language, onLanguageChange }: Props) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showWipe, setShowWipe] = useState(false);
  const [phoenixVersion, setPhoenixVersion] = useState<string | null>(null);
  const [minHaVersion, setMinHaVersion] = useState<string | null>(null);
  const [githubUrl, setGithubUrl] = useState<string | null>(null);

  useEffect(() => {
    api.getInfo().then((info) => {
      setPhoenixVersion(info.version);
      setMinHaVersion(info.min_ha_version);
      setGithubUrl(info.github_url);
    }).catch(() => {});
  }, []);

  // Deleting a provider clears the voice agent's provider server-side, and the
  // one-click Assist setup changes voice_agent_pipeline_id server-side; re-pull
  // settings on either event so the Voice Agent card reflects it.
  useEffect(() => {
    const refresh = () => api.getSettings().then(onSettingsChange).catch(() => {});
    window.addEventListener("phx-agentcli-providers-changed", refresh);
    window.addEventListener("phx-settings-refresh", refresh);
    return () => {
      window.removeEventListener("phx-agentcli-providers-changed", refresh);
      window.removeEventListener("phx-settings-refresh", refresh);
    };
  }, [onSettingsChange]);

  async function patchSetting(key: keyof GlobalSettings, value: boolean | number | string) {
    setSaving(true);
    setError(null);
    try {
      const updated = await api.patchSettings({ [key]: value });
      onSettingsChange(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("settings.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  function handleWiped() {
    setShowWipe(false);
    window.location.reload();
  }

  if (!settings) return <Loading />;

  return (
    <div className="view-root">
      {error && <div className="banner banner-error">{error}</div>}

      <div className="settings-grid">
        {/* Left column: Agent Chat, Voice Agent, AI Task, Assist Bridge */}
        <div>
          <AgentCliSettings
            scrollback={settings.agentcli_scrollback_lines}
            onScrollbackChange={(n) => patchSetting("agentcli_scrollback_lines", n)}
            maxIterations={settings.agentcli_max_iterations}
            onMaxIterationsChange={(n) => patchSetting("agentcli_max_iterations", n)}
            globalVisible={settings.agentcli_global}
            onGlobalChange={(v) => patchSetting("agentcli_global", v)}
            saving={saving}
          />

          <VoiceAgentSettings
            settings={settings}
            onChange={patchSetting}
            saving={saving}
          />

          <AiTaskSettings
            settings={settings}
            onChange={patchSetting}
            saving={saving}
          />

          <AssistBridgeSettings
            settings={settings}
            onChange={patchSetting}
            saving={saving}
          />
        </div>

        {/* Right column: Kill Switch, MESA, Experimental, Notifications, Logging, Info, Data Management */}
        <div>
          <div className="card">
            <h3 className="card-header">
              {t("settings.killSwitchCard")}
              <DocsHelpLink path="security.html#kill-switch" label={t("settings.killSwitchCard")} />
            </h3>
            <KillSwitch
              settings={settings}
              onToggle={(value) => patchSetting("kill_switch", value)}
              saving={saving}
            />
          </div>

          <div className="card">
            <h3 className="card-header">
              {t("settings.mesaCard")}
              <DocsHelpLink path="mesa.html#deployment-mode" label={t("settings.mesaCard")} />
            </h3>
            <div className="toggle-row">
              <div className="toggle-label">
                <span>{t("settings.mesaModeLabel")}</span>
                <small>
                  {t("settings.mesaModeHelp")}
                </small>
              </div>
              <select
                aria-label={t("settings.mesaModeAria")}
                className="input input-auto"
                value={settings.mesa_mode}
                disabled={saving}
                onChange={(e) => patchSetting("mesa_mode", e.target.value)}
              >
                <option value="off">{t("settings.mesaModeOff")}</option>
                <option value="advisory">{t("settings.mesaModeAdvisory")}</option>
                <option value="enforced">{t("settings.mesaModeEnforced")}</option>
              </select>
            </div>
            {settings.mesa_mode === "enforced" && (
              <div className="banner banner-warn settings-toggle-mt">
                {t("settings.mesaEnforcedWarning")}
              </div>
            )}
          </div>

          <div className="card">
            <h3 className="card-header">
              {t("settings.experimentalCard")}
              <DocsHelpLink path="panel.html#settings-tab" label={t("settings.experimentalCard")} />
            </h3>
            <div className="toggle-row toggle-row-plain">
              <div className="toggle-label">
                <span>{t("settings.presetsLabel")}</span>
                <small>
                  {t("settings.presetsHelp")}
                </small>
              </div>
              <label className={`toggle-switch${saving ? " disabled" : ""}`}>
                <input
                  type="checkbox"
                  aria-label={t("settings.presetsLabel")}
                  checked={settings.token_presets_enabled}
                  disabled={saving}
                  onChange={(e) => patchSetting("token_presets_enabled", e.target.checked)}
                />
                <span className="toggle-switch-track" />
              </label>
            </div>
            <div className="toggle-row toggle-row-plain" style={{ marginTop: 14 }}>
              <div className="toggle-label">
                <span>{t("settings.injectLabel")}</span>
                <small>
                  {t("settings.injectHelp")}
                </small>
              </div>
              <label className={`toggle-switch${saving ? " disabled" : ""}`}>
                <input
                  type="checkbox"
                  aria-label={t("settings.injectLabel")}
                  checked={settings.mesa_inject_enabled}
                  disabled={saving}
                  onChange={(e) => patchSetting("mesa_inject_enabled", e.target.checked)}
                />
                <span className="toggle-switch-track" />
              </label>
            </div>
          </div>

          <div className="card">
            <h3 className="card-header">
              {t("settings.notificationsCard")}
              <DocsHelpLink path="operations.html#global-settings" label={t("settings.notificationsCard")} />
            </h3>
            <NotificationSettings
              settings={settings}
              onToggle={patchSetting}
              saving={saving}
            />
          </div>

          <div className="card">
            <h3 className="card-header">
              {t("settings.loggingCard")}
              <DocsHelpLink path="operations.html#global-settings" label={t("settings.loggingCard")} />
            </h3>
            <LoggingSettings
              settings={settings}
              onToggle={patchSetting}
              saving={saving}
            />
            <hr className="settings-divider" />
            <div className={`toggle-row settings-toggle-mt${settings.disable_all_logging ? " toggle-row-greyed" : ""}`}>
              <div className="toggle-label">
                <span>{t("settings.flushIntervalLabel")}</span>
                <small>{t("settings.flushIntervalHelp")}</small>
              </div>
              <select
                aria-label={t("settings.flushIntervalLabel")}
                className="input input-auto"
                value={settings.audit_flush_interval}
                disabled={saving || settings.disable_all_logging}
                onChange={(e) => patchSetting("audit_flush_interval", Number(e.target.value))}
              >
                <option value={0}>{t("common.never")}</option>
                <option value={5}>{t("settings.flushEveryMinutes", { n: 5 })}</option>
                <option value={10}>{t("settings.flushEveryMinutes", { n: 10 })}</option>
                <option value={15}>{t("settings.flushEveryMinutes", { n: 15 })}</option>
                <option value={30}>{t("settings.flushEveryMinutes", { n: 30 })}</option>
                <option value={60}>{t("settings.flushEveryMinutes", { n: 60 })}</option>
              </select>
            </div>
            <div className={`toggle-row${settings.disable_all_logging ? " toggle-row-greyed" : ""}`}>
              <div className="toggle-label">
                <span>{t("settings.maxLogEntriesLabel")}</span>
                <small>{t("settings.maxLogEntriesHelp")}</small>
              </div>
              <select
                aria-label={t("settings.maxLogEntriesLabel")}
                className="input input-auto"
                value={settings.audit_log_maxlen}
                disabled={saving || settings.disable_all_logging}
                onChange={(e) => patchSetting("audit_log_maxlen", Number(e.target.value))}
              >
                <option value={100}>100</option>
                <option value={1000}>1,000</option>
                <option value={5000}>5,000</option>
                <option value={10000}>10,000</option>
              </select>
            </div>
          </div>

          <div className="card">
            <h3 className="card-header">{t("settings.infoCard")}</h3>
            <div className="settings-info-list">
              <div><strong>{t("settings.infoVersionLabel")}</strong> {phoenixVersion ?? "..."}</div>
              <div><strong>{t("settings.infoJsBuildLabel")}</strong> {JS_BUILD}</div>
              <div><strong>{t("settings.infoMinHaLabel")}</strong> {minHaVersion ?? "..."}</div>
              <div>
                <a href={githubUrl ?? "#"} target="_blank" rel="noopener noreferrer"
                  className="settings-info-link">
                  {t("settings.infoGithubLink")}
                </a>
              </div>
              <div className="settings-info-note">
                {tRich("settings.infoStorageNote", { code: (c) => <code>{c}</code> })}
              </div>
              <div className="toggle-row" style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--phx-border)" }}>
                <div className="toggle-label">
                  <span>{t("settings.languageLabel")}</span>
                  <small>{t("settings.languageHelp")}</small>
                </div>
                <select
                  aria-label={t("settings.languageLabel")}
                  className="input input-auto"
                  value={language}
                  onChange={(e) => onLanguageChange(e.target.value)}
                >
                  {/* Each language is named in itself, so someone who cannot read
                      the current UI language can still find their own. */}
                  <option value={LANGUAGE_AUTO}>{t("settings.languageAuto")}</option>
                  {LANGUAGES.map((l) => (
                    <option key={l.code} value={l.code}>{l.endonym}</option>
                  ))}
                </select>
              </div>
              <div className="toggle-row">
                <div className="toggle-label">
                  <span>{t("settings.themeLabel")}</span>
                  <small>{t("settings.themeHelp")}</small>
                </div>
                <div className="theme-toggle">
                  {(["light", "auto", "dark"] as Theme[]).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      className={`theme-toggle-btn${theme === mode ? " active" : ""}`}
                      aria-pressed={theme === mode}
                      onClick={() => onThemeChange(mode)}
                    >
                      {t(THEME_LABEL_KEYS[mode])}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="card">
            <h3 className="card-header settings-danger-header">
              {t("settings.dataCard")}
              <DocsHelpLink path="admin-api.html#wipe" label={t("settings.dataCard")} />
            </h3>
            <p className="clear-perms-body">
              {t("settings.dataBody")}
            </p>
            <button
              className="btn btn-danger"
              onClick={() => setShowWipe(true)}
            >
              {t("settings.wipeButton")}
            </button>
          </div>
        </div>
      </div>

      {showWipe && (
        <WipeConfirmModal
          onWiped={handleWiped}
          onClose={() => setShowWipe(false)}
        />
      )}
    </div>
  );
}
