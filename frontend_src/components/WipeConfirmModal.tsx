import { useState } from "react";
import { api } from "../api";
import { Modal } from "./Modal";
import { t } from "../i18n";

interface Props {
  onWiped: () => void;
  onClose: () => void;
}

export function WipeConfirmModal({ onWiped, onClose }: Props) {
  const [typed, setTyped] = useState("");
  const [wiping, setWiping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Scope toggles: the two credential-bearing scopes default on; MESA (authored
  // safety policy, not credentials) defaults off so a reset does not silently
  // discard hand-authored profiles.
  const [core, setCore] = useState(true);
  const [providers, setProviders] = useState(true);
  const [mesa, setMesa] = useState(false);

  const nothingSelected = !core && !providers && !mesa;

  async function doWipe() {
    setWiping(true);
    setError(null);
    try {
      await api.wipe({ wipe_core: core, wipe_providers: providers, wipe_mesa: mesa });
      onWiped();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("settings.wipeFailed"));
      setWiping(false);
    }
  }

  return (
    <Modal titleId="wipe-title" onClose={wiping ? undefined : onClose}>
      <h3 className="modal-title modal-title-error" id="wipe-title">
        {t("settings.wipeTitle")}
      </h3>
      <div className="banner banner-error mb-16">
        {t("settings.wipeWarning")}
      </div>

      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span>{t("settings.wipeCoreLabel")}</span>
          <small>{t("settings.wipeCoreHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input type="checkbox" aria-label={t("settings.wipeCoreLabel")} checked={core}
                 onChange={(e) => setCore(e.target.checked)} />
          <span className="toggle-switch-track" />
        </label>
      </div>

      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span>{t("settings.wipeProvidersLabel")}</span>
          <small>{t("settings.wipeProvidersHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input type="checkbox" aria-label={t("settings.wipeProvidersLabel")} checked={providers}
                 onChange={(e) => setProviders(e.target.checked)} />
          <span className="toggle-switch-track" />
        </label>
      </div>

      <div className="toggle-row toggle-row-plain">
        <div className="toggle-label">
          <span>{t("settings.wipeMesaLabel")}</span>
          <small>{t("settings.wipeMesaHelp")}</small>
        </div>
        <label className="toggle-switch">
          <input type="checkbox" aria-label={t("settings.wipeMesaLabel")} checked={mesa}
                 onChange={(e) => setMesa(e.target.checked)} />
          <span className="toggle-switch-track" />
        </label>
      </div>

      <div className="field settings-toggle-mt">
        <label htmlFor="wipe-confirm-input">{t("settings.wipeTypeConfirm")}</label>
        <input
          id="wipe-confirm-input"
          className="input"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="WIPE"
          autoFocus
        />
      </div>
      {error && <div className="banner banner-error" role="alert">{error}</div>}
      <div className="modal-actions">
        <button
          className="btn btn-danger"
          onClick={doWipe}
          disabled={typed !== "WIPE" || wiping || nothingSelected}
        >
          {wiping ? t("settings.wipeBusy") : t("settings.wipeSubmit")}
        </button>
        <button className="btn btn-text" onClick={onClose} disabled={wiping}>{t("settings.cancel")}</button>
      </div>
    </Modal>
  );
}
