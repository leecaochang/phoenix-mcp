import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const getSettings = vi.fn();
const patchSettings = vi.fn();

vi.mock("../api", () => ({
  api: {
    getInfo: () => Promise.resolve({ version: "1.0.0", min_ha_version: "2025.2.0", github_url: "#" }),
    getSettings: (...args: unknown[]) => getSettings(...args),
    patchSettings: (...args: unknown[]) => patchSettings(...args),
    listTokens: () => Promise.resolve([]),
    getAgentCliProviders: () => Promise.resolve({ instances: [] }),
    getAgentCliModels: () => Promise.resolve({ models: [] }),
    getAiTaskPreferred: () => Promise.resolve({ supported: false }),
    getAssistStatus: () => Promise.resolve({ supported: false }),
    getVoiceAgentPipeline: () => Promise.resolve({ supported: false }),
  },
}));

import { SettingsView } from "../views/SettingsView";
import type { GlobalSettings } from "../types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("SettingsView request ordering", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getSettings.mockReset();
    patchSettings.mockReset();
  });

  function renderSettings(onSettingsChange = vi.fn()) {
    render(
      <SettingsView
        settings={{ kill_switch: false } as GlobalSettings}
        onSettingsChange={onSettingsChange}
        theme="auto"
        onThemeChange={vi.fn()}
        language="auto"
        onLanguageChange={vi.fn()}
      />,
    );
    return {
      killSwitch: screen.getByRole("checkbox", { name: "Disable all Phoenix MCP endpoints" }),
      onSettingsChange,
    };
  }

  it("discards an older refresh response that finishes last", async () => {
    const stale = deferred<GlobalSettings>();
    const current = deferred<GlobalSettings>();
    getSettings.mockReturnValueOnce(stale.promise).mockReturnValueOnce(current.promise);
    const onSettingsChange = vi.fn();

    renderSettings(onSettingsChange);

    window.dispatchEvent(new CustomEvent("phx-settings-refresh"));
    window.dispatchEvent(new CustomEvent("phx-settings-refresh"));
    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(2));

    const latestSettings = { kill_switch: false } as GlobalSettings;
    current.resolve(latestSettings);
    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledWith(latestSettings));

    stale.resolve({ kill_switch: true } as GlobalSettings);
    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledTimes(1));
  });

  it("queues a refresh behind an in-flight PATCH and always clears saving", async () => {
    const mutation = deferred<GlobalSettings>();
    const refresh = deferred<GlobalSettings>();
    patchSettings.mockReturnValueOnce(mutation.promise);
    getSettings.mockReturnValueOnce(refresh.promise);
    const { killSwitch, onSettingsChange } = renderSettings();

    fireEvent.click(killSwitch);
    await waitFor(() => expect(patchSettings).toHaveBeenCalledWith({ kill_switch: true }));
    expect(killSwitch).toBeDisabled();
    window.dispatchEvent(new CustomEvent("phx-settings-refresh"));
    expect(getSettings).not.toHaveBeenCalled();

    const patched = { kill_switch: true } as GlobalSettings;
    mutation.resolve(patched);
    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledWith(patched));
    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(1));
    expect(killSwitch).not.toBeDisabled();

    const refreshed = {
      kill_switch: true,
      mesa_mode: "enforce",
    } as unknown as GlobalSettings;
    refresh.resolve(refreshed);
    await waitFor(() => expect(onSettingsChange).toHaveBeenLastCalledWith(refreshed));
  });

  it("discards a refresh that began before a PATCH", async () => {
    const staleRefresh = deferred<GlobalSettings>();
    const mutation = deferred<GlobalSettings>();
    getSettings.mockReturnValueOnce(staleRefresh.promise);
    patchSettings.mockReturnValueOnce(mutation.promise);
    const { killSwitch, onSettingsChange } = renderSettings();

    window.dispatchEvent(new CustomEvent("phx-settings-refresh"));
    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(1));
    fireEvent.click(killSwitch);
    await waitFor(() => expect(patchSettings).toHaveBeenCalledTimes(1));

    staleRefresh.resolve({ kill_switch: false } as GlobalSettings);
    await Promise.resolve();
    expect(onSettingsChange).not.toHaveBeenCalled();

    const patched = { kill_switch: true } as GlobalSettings;
    mutation.resolve(patched);
    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledTimes(1));
    expect(onSettingsChange).toHaveBeenCalledWith(patched);
    expect(killSwitch).not.toBeDisabled();
  });

  it("clears saving even when a queued refresh fails", async () => {
    const mutation = deferred<GlobalSettings>();
    patchSettings.mockReturnValueOnce(mutation.promise);
    getSettings.mockRejectedValueOnce(new Error("refresh failed"));
    const { killSwitch } = renderSettings();

    fireEvent.click(killSwitch);
    await waitFor(() => expect(patchSettings).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new CustomEvent("phx-settings-refresh"));
    mutation.resolve({ kill_switch: true } as GlobalSettings);

    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(killSwitch).not.toBeDisabled());
  });
});
