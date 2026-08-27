import type { ConversationStyle, DetailLevel } from "../types";
import { t } from "../i18n";

const STYLES: ConversationStyle[] = ["direct", "warm", "calm_guide", "lively", "technical"];
const DETAILS: DetailLevel[] = ["concise", "balanced", "detailed"];

const STYLE_LABELS: Record<ConversationStyle, string> = {
  direct: "settings.styleDirect",
  warm: "settings.styleWarm",
  calm_guide: "settings.styleCalmGuide",
  lively: "settings.styleLively",
  technical: "settings.styleTechnical",
};

const STYLE_HELP: Record<ConversationStyle, string> = {
  direct: "settings.styleDirectHelp",
  warm: "settings.styleWarmHelp",
  calm_guide: "settings.styleCalmGuideHelp",
  lively: "settings.styleLivelyHelp",
  technical: "settings.styleTechnicalHelp",
};

const DETAIL_LABELS: Record<DetailLevel, string> = {
  concise: "settings.detailConcise",
  balanced: "settings.detailBalanced",
  detailed: "settings.detailDetailed",
};

const DETAIL_HELP: Record<DetailLevel, string> = {
  concise: "settings.detailConciseHelp",
  balanced: "settings.detailBalancedHelp",
  detailed: "settings.detailDetailedHelp",
};

export function ConversationBehaviorControls({
  surface,
  style,
  detail,
  homeFocused,
  saving,
  onStyleChange,
  onDetailChange,
  onHomeFocusedChange,
  aiTaskFreeTextOnly = false,
}: {
  surface: string;
  style: ConversationStyle;
  detail: DetailLevel;
  homeFocused?: boolean;
  saving: boolean;
  onStyleChange: (value: ConversationStyle) => void;
  onDetailChange: (value: DetailLevel) => void;
  onHomeFocusedChange?: (value: boolean) => void;
  aiTaskFreeTextOnly?: boolean;
}) {
  return (
    <>
      <hr className="settings-divider" />
      <div className="toggle-row toggle-row-stacked-control">
        <div className="toggle-label">
          <span>{t("settings.conversationStyle")}</span>
          <small>{t(STYLE_HELP[style])}</small>
        </div>
        <select
          className="input input-auto"
          value={style}
          disabled={saving}
          aria-label={t("settings.conversationStyleAria", { surface })}
          onChange={(event) => onStyleChange(event.target.value as ConversationStyle)}
        >
          {STYLES.map((value) => (
            <option key={value} value={value}>{t(STYLE_LABELS[value])}</option>
          ))}
        </select>
      </div>
      <div className="toggle-row toggle-row-stacked-control">
        <div className="toggle-label">
          <span>{t("settings.detailLevel")}</span>
          <small>
            {t(DETAIL_HELP[detail])}
            {aiTaskFreeTextOnly ? ` ${t("settings.aiTaskBehaviorFreeTextOnly")}` : ""}
          </small>
        </div>
        <select
          className="input input-auto"
          value={detail}
          disabled={saving}
          aria-label={t("settings.detailLevelAria", { surface })}
          onChange={(event) => onDetailChange(event.target.value as DetailLevel)}
        >
          {DETAILS.map((value) => (
            <option key={value} value={value}>{t(DETAIL_LABELS[value])}</option>
          ))}
        </select>
      </div>
      {homeFocused !== undefined && onHomeFocusedChange && (
        <div className="toggle-row">
          <div className="toggle-label">
            <span>{t("settings.homeFocused")}</span>
            <small>{t("settings.homeFocusedHelp")}</small>
          </div>
          <label className={`toggle-switch${saving ? " disabled" : ""}`}>
            <input
              type="checkbox"
              checked={homeFocused}
              disabled={saving}
              aria-label={t("settings.homeFocusedAria", { surface })}
              onChange={(event) => onHomeFocusedChange(event.target.checked)}
            />
            <span className="toggle-switch-track" />
          </label>
        </div>
      )}
    </>
  );
}
