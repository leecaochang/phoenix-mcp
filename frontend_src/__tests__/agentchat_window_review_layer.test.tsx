import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  api: {
    listTokens: vi.fn().mockResolvedValue([]),
    getAgentCliProviders: vi.fn().mockResolvedValue({ instances: [] }),
    getSettings: vi.fn().mockResolvedValue({ kill_switch: false, agentcli_scrollback_lines: 500 }),
  },
  setHass: vi.fn(),
}));

vi.mock("../i18n", () => ({
  loadTranslations: vi.fn().mockResolvedValue(undefined),
  resolveLanguage: vi.fn().mockReturnValue("en"),
  syncTranslations: vi.fn().mockResolvedValue(false),
}));

vi.mock("../components/AgentCliWindow", () => ({
  AgentCliWindow: () => null,
  focusAgentCliPopup: vi.fn().mockReturnValue(false),
}));

import { hideAgentChat, showAgentChat } from "../inject/AgentChatWindow";
import {
  notifyAgentChatReviewClosed,
  notifyAgentChatReviewOpened,
} from "../utils/agentchat_review";

describe("global Agent Chat approval-review layer", () => {
  afterEach(() => {
    hideAgentChat();
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it("detaches the fixed host during review and restores it on the next frame", () => {
    const resumes: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      resumes.push(callback);
      return 1;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});

    showAgentChat("token-1");
    const host = document.body.lastElementChild as HTMLElement;
    expect(host).toBeTruthy();
    expect(host.dataset.jsBuild).toBeTruthy();

    notifyAgentChatReviewOpened("approval-1");
    expect(host.isConnected).toBe(false);

    notifyAgentChatReviewClosed("approval-1");
    expect(host.isConnected).toBe(false);
    expect(resumes).toHaveLength(1);

    resumes[0](performance.now());
    expect(host.isConnected).toBe(true);
    expect(document.body.lastElementChild).toBe(host);
  });
});
