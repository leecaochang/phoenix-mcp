import { useEffect, useState } from "react";
import type { GlobalSettings, TokenRecord } from "../types";
import { api } from "../api";
import { DocsHelpLink } from "./common";
import { t } from "../i18n";
import { tRich } from "../i18n/rich";

// Assist tool-provider card ("Phoenix MCP (scoped)"). Binds ONE token whose scoped tools
// Phoenix MCP hands to a conversation agent supplied by another integration (OpenAI,
// Anthropic, Ollama, and so on). Distinct from the Voice Agent card, which runs
// Phoenix MCP's own model. Install-wide singleton, so it is a Settings-level control (a
// bound-token dropdown), not a per-token toggle.
export function AssistBridgeSettings({
  settings,
  onChange,
  saving,
}: {
  settings: GlobalSettings;
  onChange: (key: keyof GlobalSettings, value: boolean | string) => void;
  saving: boolean;
}) {
  const [tokens, setTokens] = useState<TokenRecord[]>([]);
  const supported = settings.assist_api_supported !== false;
  const boundId = settings.assist_bound_token_id ?? "";

  useEffect(() => {
    api.listTokens().then(setTokens).catch(() => setTokens([]));
  }, []);

  return (
    <div className="card">
      <h3 className="card-header">
        {t("settings.assistCard")}
        <DocsHelpLink path="connect.html#assist-tool-provider" label={t("settings.assistCard")} />
      </h3>
      <p className="agentcli-settings-hint">
        {tRich("settings.assistIntro", { strong: (c) => <strong>{c}</strong> })}
      </p>
      <div className="toggle-row">
        <div className="toggle-label">
          <span>{t("settings.assistBoundToken")}</span>
          <small>
            {supported ? (
              tRich("settings.assistBoundHelp", { strong: (c) => <strong>{c}</strong> })
            ) : (
              <em>{t("settings.assistUnsupported")}</em>
            )}
          </small>
        </div>
        <select
          aria-label={t("settings.assistBoundTokenAria")}
          className="input input-auto"
          value={boundId}
          disabled={saving || !supported}
          onChange={(e) => onChange("assist_bound_token_id", e.target.value)}
        >
          <option value="">{t("settings.assistNotBound")}</option>
          {tokens.map((tok) => (
            <option key={tok.id} value={tok.id}>{tok.name}</option>
          ))}
        </select>
      </div>
    </div>
  );
}
