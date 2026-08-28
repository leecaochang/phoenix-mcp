/** Global Agent Chat waits for Home Assistant authentication to finish. */

import { afterEach, describe, expect, it, vi } from "vitest";

const ACTIVE_FLAG = "__phxAgentChatBootstrapped";

describe("global Agent Chat bootstrap", () => {
  afterEach(() => {
    const controller = (window as unknown as Record<string, unknown>)[ACTIVE_FLAG] as
      | { dispose?: () => void }
      | undefined;
    controller?.dispose?.();
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.replaceChildren();
    delete (window as unknown as Record<string, unknown>)[ACTIVE_FLAG];
    delete (window as unknown as Record<string, unknown>).__phxAgentChat;
    vi.resetModules();
  });

  it("installs after hass.user arrives later than the module", async () => {
    vi.useFakeTimers();
    const homeAssistant = document.createElement("home-assistant") as HTMLElement & {
      hass: Record<string, unknown>;
    };
    homeAssistant.hass = {};
    document.body.appendChild(homeAssistant);

    await import("../inject/agentchat");
    expect((window as unknown as Record<string, unknown>).__phxAgentChat).toBeUndefined();

    homeAssistant.hass = {
      enableShortcuts: true,
      user: { is_admin: true },
    };
    await vi.advanceTimersByTimeAsync(250);

    expect((window as unknown as Record<string, unknown>).__phxAgentChat)
      .toMatchObject({
        ready: true,
        isVisible: expect.any(Function),
        restore: expect.any(Function),
      });
  });
});
