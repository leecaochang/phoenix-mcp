/** The Agent Chat conversation survives a full page reload.
 *
 * Two tiers back the window. Window state (geometry, token/provider/model) has
 * always been durable. The conversation tier -- transcript, unsent message box,
 * usage counters -- used to be module variables, so it survived the panel
 * unmounting but not F5.
 *
 * It is now persisted under its own storage key, separate from the window
 * state, so that a conversation too large to store can be dropped without
 * taking the window geometry and account selections down with it.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  getSessionTurns, setSessionTurns,
  getSessionDraft, setSessionDraft,
  getSessionUsage, setSessionUsage,
  agentCliOpenPatch, getDurable, patchDurable,
  __resetAgentCliState, __reloadFromStorage,
} from "../utils/agentcli_state";

const CONV_KEY = "phx-agentcli-conversation";

beforeEach(() => __resetAgentCliState());
afterEach(() => vi.restoreAllMocks());

describe("conversation survives a reload", () => {
  it("restores transcript, draft, and usage", () => {
    setSessionTurns([{ id: 1 }, { id: 2 }]);
    setSessionDraft("turn off the kitchen lights");
    setSessionUsage({ input: 120, output: 45, context: 120, noData: false });

    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([{ id: 1 }, { id: 2 }]);
    expect(getSessionDraft()).toBe("turn off the kitchen lights");
    expect(getSessionUsage()).toEqual({ input: 120, output: 45, context: 120, noData: false });
  });

  it("clearing the history clears the stored copy too", () => {
    setSessionTurns([{ id: 1 }]);
    setSessionTurns([]);              // what "Clear chat history" / "/clear" does

    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([]);
  });

  it("keeps the conversation separate from the window state", () => {
    patchDurable({ size: { w: 700, h: 500 }, tokenId: "t1" });
    setSessionTurns([{ id: 1 }]);
    // Losing the conversation must not disturb geometry or account selection.
    localStorage.removeItem(CONV_KEY);
    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([]);
    expect(getDurable().size).toEqual({ w: 700, h: 500 });
    expect(getDurable().tokenId).toBe("t1");
  });
});

describe("Agent Chat open geometry", () => {
  it("preserves the last dragged position for a shortcut reopen", () => {
    patchDurable({
      open: false,
      minimized: true,
      pos: { x: 137, y: 246 },
      size: { w: 620, h: 480 },
    });

    patchDurable(agentCliOpenPatch(getDurable()));

    expect(getDurable()).toMatchObject({
      open: true,
      minimized: false,
      pos: { x: 137, y: 246 },
      size: { w: 620, h: 480 },
    });
  });

  it("centers a button-driven summon in the supplied viewport", () => {
    patchDurable({ pos: { x: 137, y: 246 }, size: { w: 620, h: 480 } });

    patchDurable(agentCliOpenPatch(getDurable(), "token-2", { w: 1200, h: 900 }));

    expect(getDurable()).toMatchObject({
      open: true,
      minimized: false,
      pos: { x: 290, y: 210 },
      tokenId: "token-2",
    });
  });
});

describe("degrades safely", () => {
  it("drops the oldest turns when the transcript will not fit", () => {
    // Each turn ~200KB; well past the 2MB cap at 20 of them.
    const big = (id: number) => ({ id, blob: "x".repeat(200_000) });
    setSessionTurns(Array.from({ length: 20 }, (_, i) => big(i)));

    __reloadFromStorage();
    const kept = getSessionTurns() as { id: number }[];

    expect(kept.length).toBeGreaterThan(0);
    expect(kept.length).toBeLessThan(20);
    // The NEWEST turns are the ones kept.
    expect(kept[kept.length - 1].id).toBe(19);
    expect(JSON.stringify(kept).length).toBeLessThanOrEqual(2_000_000);
  });

  it("keeps the draft even when no transcript fits at all", () => {
    setSessionDraft("still here");
    setSessionTurns([{ blob: "x".repeat(3_000_000) }]);   // one turn, over cap alone

    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([]);
    expect(getSessionDraft()).toBe("still here");
  });

  it("starts clean on corrupt stored data instead of throwing", () => {
    localStorage.setItem(CONV_KEY, "{not json");
    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([]);
    expect(getSessionDraft()).toBe("");
  });

  it("ignores a stored payload of the wrong shape", () => {
    localStorage.setItem(CONV_KEY, JSON.stringify({ turns: "nope", draft: 42 }));
    __reloadFromStorage();

    expect(getSessionTurns()).toEqual([]);
    expect(getSessionDraft()).toBe("");
  });

  it("does not throw when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(() => setSessionDraft("no storage here")).not.toThrow();
    // Still readable in memory for this page load.
    expect(getSessionDraft()).toBe("no storage here");
  });
});
