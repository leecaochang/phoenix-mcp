import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, screen, waitFor, within } from "@testing-library/react";

// Mock the API module: agentCliChat drives the window's event handling, and the
// admin api methods back the model list + approval buttons.
const agentCliChat = vi.fn();
const getAgentCliModels = vi.fn();
const approveApproval = vi.fn();
const rejectApproval = vi.fn();

vi.mock("../api", () => ({
  agentCliChat: (...args: unknown[]) => agentCliChat(...args),
  api: {
    getAgentCliModels: (...args: unknown[]) => getAgentCliModels(...args),
    approveApproval: (...args: unknown[]) => approveApproval(...args),
    rejectApproval: (...args: unknown[]) => rejectApproval(...args),
  },
}));

import {
  AgentCliWindow,
  trimTurns,
  memoryMessages,
  buildOptions,
  modelCaps,
  snapResizeRect,
  clampPosToViewport,
  resolveAgentCliTopMargin,
  fmtTokens,
  focusAgentCliPopup,
  type Turn,
} from "../components/AgentCliWindow";
import { getDurable, patchDurable, setSessionTurns, __resetAgentCliState } from "../utils/agentcli_state";
import { clearReasonDraft, getReasonDraft, setReasonDraft } from "../utils/approval_reason_draft";
import type { AgentCliInstance, TokenRecord } from "../types";
import { setFormatLocale } from "../i18n";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const TOKENS = [
  { id: "t1", name: "alpha" } as TokenRecord,
  { id: "t2", name: "beta" } as TokenRecord,
];
const INSTANCES: AgentCliInstance[] = [
  { id: "i-claude", kind: "claude", name: "Claude", model: "claude-opus-4-8" },
];

function createPopupWindow(): { popup: Window; iframe: HTMLIFrameElement } {
  const iframe = document.createElement("iframe");
  document.body.appendChild(iframe);
  const popup = iframe.contentWindow;
  if (!popup) throw new Error("iframe popup window unavailable");
  vi.spyOn(popup, "focus").mockImplementation(() => {});
  return { popup, iframe };
}

function mkTurn(lines: number, msgs: unknown[]): Turn {
  return { entries: [], messages: msgs, lines };
}

describe("agentCLI resize snapping (snapResizeRect)", () => {
  const VW = 1000, VH = 800, M = 8;
  // A window snapped to the top-left, small.
  const o = { x: 8, y: 8, w: 400, h: 300 };

  it("snaps the bottom edge to the viewport margin when stretched past it", () => {
    // Drag the south edge far down (dy huge) -> bottom edge clamps to VH - M.
    const r = snapResizeRect("s", o, 0, 5000, VW, VH);
    expect(r.y).toBe(8);              // top edge unchanged
    expect(r.y + r.h).toBe(VH - M);   // bottom edge pinned to the margin
  });

  it("snaps the right edge to the viewport margin when stretched past it", () => {
    const r = snapResizeRect("e", o, 5000, 0, VW, VH);
    expect(r.x).toBe(8);
    expect(r.x + r.w).toBe(VW - M);
  });

  it("snaps the left and top edges to the margin, keeping the opposite edge fixed", () => {
    const start = { x: 400, y: 400, w: 400, h: 300 };
    const rightAnchor = start.x + start.w, bottomAnchor = start.y + start.h;
    const r = snapResizeRect("nw", start, -5000, -5000, VW, VH);
    expect(r.x).toBe(M);                       // left edge snapped to margin
    expect(r.y).toBe(M);                       // top edge snapped to margin
    expect(r.x + r.w).toBe(rightAnchor);       // right edge stayed put
    expect(r.y + r.h).toBe(bottomAnchor);      // bottom edge stayed put
  });

  it("does not snap when the edge is nowhere near the boundary", () => {
    const r = snapResizeRect("se", o, 100, 50, VW, VH);
    expect(r.w).toBe(500);   // 400 + 100, no clamp
    expect(r.h).toBe(350);   // 300 + 50, no clamp
  });

  it("never shrinks below the minimum window size", () => {
    const r = snapResizeRect("se", o, -5000, -5000, VW, VH);
    expect(r.w).toBe(320);
    expect(r.h).toBe(280);
  });
});

describe("agentCLI pill remembers its own position", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  function dialog(): HTMLElement {
    return screen.getByRole("dialog", { name: "Agent Chat" });
  }

  it("renders the minimized pill at its own remembered position, not the window's", async () => {
    // Window lives top-left; the pill was last left in the middle.
    patchDurable({ open: true, minimized: true, pos: { x: 8, y: 8 }, pillPos: { x: 400, y: 300 } });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    const el = dialog();
    expect(el.style.left).toBe("400px");
    expect(el.style.top).toBe("300px");
  });

  it("restoring then minimizing again returns the pill to where it was", async () => {
    patchDurable({ open: true, minimized: true, pos: { x: 8, y: 8 },
                   size: { w: 440, h: 560 }, pillPos: { x: 400, y: 300 } });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    // Restore: the window returns to its own position (8,8), not the pill's.
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(dialog().style.width).toBe("440px"));
    expect(dialog().style.left).toBe("8px");

    // Minimize again: the pill comes back to its remembered spot.
    fireEvent.click(screen.getByRole("button", { name: "Minimize" }));
    const el = dialog();
    expect(el.style.left).toBe("400px");
    expect(el.style.top).toBe("300px");
    expect(el.style.width).toBe("");  // pill: no inline width, CSS drives it
  });

  it("restores the live minimized window when the Agent Chat button summons it", async () => {
    patchDurable({
      open: true,
      minimized: true,
      pos: { x: 8, y: 8 },
      size: { w: 440, h: 560 },
      pillPos: { x: 400, y: 300 },
    });
    const view = render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} summonVersion={0} />,
    );
    expect(dialog()).toHaveClass("agentcli-minimized");

    // Button-driven opening writes its requested geometry first. The injected
    // host then bumps the signal on the already-mounted component.
    patchDurable({ minimized: false, pos: { x: 260, y: 120 } });
    view.rerender(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} summonVersion={1} />,
    );

    await waitFor(() => expect(dialog()).not.toHaveClass("agentcli-minimized"));
    expect(dialog().style.left).toBe("260px");
    expect(dialog().style.top).toBe("120px");
    expect(dialog().style.width).toBe("440px");
  });
});

describe("AgentCliWindow latest-message scrolling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  it("returns to the bottom after restore, resize, pop-out, and pop-in", async () => {
    const scrollHeight = 1200;
    setSessionTurns([{
      entries: Array.from({ length: 30 }, (_, index) => ({
        kind: "assistant",
        text: `Reply ${index}`,
        thinking: "",
      })),
      messages: [],
      lines: 30,
    }]);
    patchDurable({
      open: true,
      minimized: true,
      pos: { x: 8, y: 8 },
      size: { w: 440, h: 560 },
      pillPos: { x: 400, y: 300 },
    });
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("agentcli-body") ? scrollHeight : 0;
    });
    const { popup, iframe } = createPopupWindow();
    const PopupHTMLElement = (popup as Window & { HTMLElement: typeof HTMLElement }).HTMLElement;
    vi.spyOn(PopupHTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("agentcli-body") ? scrollHeight : 0;
    });
    vi.spyOn(window, "open").mockReturnValue(popup);

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    const dialog = screen.getByRole("dialog", { name: "Agent Chat" });
    const inAppBody = dialog.querySelector<HTMLElement>(".agentcli-body");
    if (!inAppBody) throw new Error("Agent Chat transcript not found");
    await waitFor(() => expect(inAppBody.scrollTop).toBe(scrollHeight));

    inAppBody.scrollTop = 0;
    const resizeHandle = dialog.querySelector<HTMLElement>(".agentcli-resize-se");
    if (!resizeHandle) throw new Error("Agent Chat resize handle not found");
    fireEvent.pointerDown(resizeHandle, { clientX: 400, clientY: 500 });
    fireEvent.pointerMove(window, { clientX: 420, clientY: 520 });
    fireEvent.pointerUp(window);
    await waitFor(() => expect(inAppBody.scrollTop).toBe(scrollHeight));

    inAppBody.scrollTop = 0;
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(inAppBody.scrollTop).toBe(scrollHeight));

    inAppBody.scrollTop = 0;
    fireEvent.click(screen.getByRole("button", { name: "Pop out to a separate window" }));
    const popupUi = within(popup.document.body);
    const popupBody = popupUi.getByRole("dialog", { name: "Agent Chat" })
      .querySelector<HTMLElement>(".agentcli-body");
    if (!popupBody) throw new Error("Popped-out Agent Chat transcript not found");
    await waitFor(() => expect(popupBody.scrollTop).toBe(scrollHeight));

    popupBody.scrollTop = 0;
    fireEvent.click(popupUi.getByRole("button", { name: "Pop back into Home Assistant" }));
    const restoredBody = await waitFor(() => {
      const body = screen.getByRole("dialog", { name: "Agent Chat" })
        .querySelector<HTMLElement>(".agentcli-body");
      expect(body).not.toBeNull();
      return body as HTMLElement;
    });
    await waitFor(() => expect(restoredBody.scrollTop).toBe(scrollHeight));
    iframe.remove();
  });
});

describe("agentCLI restore repositioning (clampPosToViewport)", () => {
  const VW = 1000, VH = 800, M = 8;

  it("pins a full-size window to the top-left margin (maximized case)", () => {
    // A maximized window (fills the viewport) whose pill was dragged to the
    // middle must unfold back into the corner, not off-screen from the middle.
    const size = { w: VW - 2 * M, h: VH - 2 * M };
    const np = clampPosToViewport({ x: 500, y: 400 }, size, VW, VH);
    expect(np).toEqual({ x: M, y: M });
  });

  it("shifts an off-screen but fittable window just enough to fit", () => {
    const size = { w: 400, h: 300 };
    // Dragged near the bottom-right; restore shifts it left/up to sit fully in.
    const np = clampPosToViewport({ x: 900, y: 700 }, size, VW, VH);
    expect(np).toEqual({ x: VW - 400 - M, y: VH - 300 - M });
  });

  it("leaves an already-fitting window where it is", () => {
    const size = { w: 400, h: 300 };
    const np = clampPosToViewport({ x: 120, y: 90 }, size, VW, VH);
    expect(np).toEqual({ x: 120, y: 90 });
  });

  it("uses a larger safe top when mobile browser chrome occupies the viewport", () => {
    const mobileTop = 76;
    const size = { w: 400, h: 300 };
    expect(clampPosToViewport({ x: 120, y: 0 }, size, VW, VH, mobileTop))
      .toEqual({ x: 120, y: mobileTop });
  });

  it("uses Home Assistant's exact safe top without an arbitrary mobile floor", () => {
    expect(resolveAgentCliTopMargin("47px")).toBe(47);
    expect(resolveAgentCliTopMargin("20px")).toBe(20);
    expect(resolveAgentCliTopMargin("0px")).toBe(8);
    expect(resolveAgentCliTopMargin("")).toBe(8);
  });
});

describe("agentCLI scrollback / memory bounds", () => {
  it("scrollback 0 keeps only the last turn for display and sends no memory", () => {
    const turns = [mkTurn(3, [{ a: 1 }]), mkTurn(3, [{ b: 2 }])];
    expect(trimTurns(turns, 0)).toEqual([turns[1]]);
    expect(memoryMessages(turns, 0)).toEqual([]);
  });

  it("scrollback N keeps whole newest turns within the line budget", () => {
    const turns = [mkTurn(4, [{ a: 1 }]), mkTurn(4, [{ b: 2 }]), mkTurn(4, [{ c: 3 }])];
    // budget 6 -> only the last turn fits without exceeding (4 <= 6; +4 would be 8)
    expect(trimTurns(turns, 6)).toEqual([turns[2]]);
    // budget 10 -> last two turns (8 <= 10). Memory is the flattened kept turns
    // (the component always stores turns already trimmed to budget).
    const kept = trimTurns(turns, 10);
    expect(kept).toEqual([turns[1], turns[2]]);
    expect(memoryMessages(kept, 10)).toEqual([{ b: 2 }, { c: 3 }]);
  });

  it("always keeps at least one turn even if it exceeds the budget", () => {
    const turns = [mkTurn(50, [{ big: true }])];
    expect(trimTurns(turns, 10)).toEqual([turns[0]]);
  });
});

const vals = (caps: ReturnType<typeof modelCaps>) => caps.thinking.map((t) => t.value);

describe("agentCLI model capabilities (real per-provider levels)", () => {
  it("claude: off + low..max effort, no temperature", () => {
    const c = modelCaps("claude", "claude-opus-4-8", true);
    expect(c.style).toBe("effort");
    expect(c.temperature).toBe(false);
    expect(vals(c)).toEqual(["off", "low", "medium", "high", "xhigh", "max"]);
  });
  it("deepseek: off/low/high/max; temperature only when thinking off", () => {
    // `low` is real on the v4 models. The retired deepseek-chat / deepseek-reasoner
    // aliases remapped low and medium to high, and the reasoner branch that used to
    // sit here described a model that no longer answers.
    expect(vals(modelCaps("deepseek", "deepseek-v4-flash", true))).toEqual(["off", "low", "high", "max"]);
    expect(modelCaps("deepseek", "deepseek-v4-flash", true).temperature).toBe(false);
    expect(modelCaps("deepseek", "deepseek-v4-flash", false).temperature).toBe(true);
    // No model-NAME special cases left: every deepseek model gets the same set,
    // which is what makes the capability refresh the thing that corrects it.
    expect(vals(modelCaps("deepseek", "deepseek-v4-pro", true))).toEqual(["off", "low", "high", "max"]);
  });
  it("chatgpt: gpt-5 none..high, o-series low..high, plain gpt-* no thinking + temperature", () => {
    expect(vals(modelCaps("chatgpt", "gpt-5", false))).toEqual(["none", "minimal", "low", "medium", "high"]);
    expect(vals(modelCaps("chatgpt", "o3-mini", false))).toEqual(["low", "medium", "high"]);
    expect(modelCaps("chatgpt", "o3-mini", false).temperature).toBe(false);
    const g = modelCaps("chatgpt", "gpt-4o", false);
    expect(g.thinking).toEqual([]);
    expect(g.temperature).toBe(true);
  });
  it("ollama: boolean on/off + temperature", () => {
    const o = modelCaps("ollama", "llama3", false);
    expect(o.style).toBe("boolean");
    expect(vals(o)).toEqual(["off", "on"]);
    expect(o.temperature).toBe(true);
  });
  it("gemini: 2.5/3.x expose thinking_level (minimal..high), never temperature", () => {
    for (const model of ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3-pro"]) {
      const g = modelCaps("gemini", model, false);
      expect(vals(g)).toEqual(["minimal", "low", "medium", "high"]);
      expect(g.temperature).toBe(false);
    }
    const g15 = modelCaps("gemini", "gemini-1.5-pro", false);
    expect(g15.thinking).toEqual([]);
    expect(g15.temperature).toBe(false);
  });
  it("grok: off/low/high effort; temperature only when thinking off", () => {
    const off = modelCaps("grok", "grok-4", false);
    expect(off.style).toBe("effort");
    expect(vals(off)).toEqual(["off", "low", "high"]);
    expect(off.temperature).toBe(true);
    expect(modelCaps("grok", "grok-4", true).temperature).toBe(false);
  });
  it("kimi: k3 effort levels, k2 boolean toggle, moonshot-v1 temperature only", () => {
    // K3 always reasons, so there is no off; low/high/max are the levels it takes.
    const k3 = modelCaps("kimi", "kimi-k3", true);
    expect(k3.style).toBe("effort");
    expect(vals(k3)).toEqual(["low", "high", "max"]);
    expect(k3.temperature).toBe(false);
    // The 1M-context variant is the same model family.
    expect(vals(modelCaps("kimi", "kimi-k3[1m]", true))).toEqual(["low", "high", "max"]);
    // K2.x has no effort levels, just a thinking toggle.
    const k2 = modelCaps("kimi", "kimi-k2.6", true);
    expect(k2.style).toBe("boolean");
    expect(vals(k2)).toEqual(["off", "on"]);
    expect(k2.temperature).toBe(false);
    // Only the legacy models take temperature, and they have no thinking control.
    const legacy = modelCaps("kimi", "moonshot-v1-128k", false);
    expect(legacy.thinking).toEqual([]);
    expect(legacy.temperature).toBe(true);
  });
  it("meta: effort levels with no off and no max, never temperature", () => {
    // Muse Spark rejects reasoning_effort "none", and "max" is not a Meta level.
    const caps = modelCaps("meta", "muse-spark-1.1", true);
    expect(caps.style).toBe("effort");
    expect(vals(caps)).toEqual(["minimal", "low", "medium", "high", "xhigh"]);
    expect(caps.temperature).toBe(false);
  });
  it("minimax: boolean thinking toggle, never temperature", () => {
    const caps = modelCaps("minimax", "MiniMax-M2", true);
    expect(caps.style).toBe("boolean");
    expect(vals(caps)).toEqual(["off", "on"]);
    expect(caps.temperature).toBe(false);
  });
  it("ollama cloud matches local: boolean think + temperature", () => {
    const caps = modelCaps("ollama_cloud", "gpt-oss:120b", false);
    expect(caps.style).toBe("boolean");
    expect(vals(caps)).toEqual(["off", "on"]);
    expect(caps.temperature).toBe(true);
  });
  it("openrouter: no thinking control, temperature only", () => {
    const caps = modelCaps("openrouter", "meta-llama/llama-3.3-70b-instruct", false);
    expect(caps.thinking).toEqual([]);
    expect(caps.temperature).toBe(true);
  });
  it("nvidia: no thinking control, temperature only", () => {
    const caps = modelCaps("nvidia", "meta/llama-3.3-70b-instruct", false);
    expect(caps.thinking).toEqual([]);
    expect(caps.temperature).toBe(true);
  });
});

describe("agentCLI generation options mapping", () => {
  it("claude sends thinking + effort; verbose maps to show_thinking", () => {
    const caps = modelCaps("claude", "claude-opus-4-8", true);
    expect(buildOptions("claude", { thinking: true, effort: "xhigh", temperature: "", verbose: false }, caps))
      .toEqual({ thinking: true, effort: "xhigh", show_thinking: false });
  });
  it("deepseek: thinking on -> toggle + effort, no temperature; off -> temperature", () => {
    const on = modelCaps("deepseek", "deepseek-chat", true);
    expect(buildOptions("deepseek", { thinking: true, effort: "max", temperature: "0.4", verbose: true }, on))
      .toEqual({ thinking: true, effort: "max", show_thinking: true });
    const off = modelCaps("deepseek", "deepseek-chat", false);
    expect(buildOptions("deepseek", { thinking: false, effort: "high", temperature: "0.4", verbose: false }, off))
      .toEqual({ thinking: false, temperature: 0.4, show_thinking: false });
  });
  it("chatgpt reasoning model sends reasoning effort only; plain model sends temperature", () => {
    const reason = modelCaps("chatgpt", "o3-mini", false);
    expect(buildOptions("chatgpt", { thinking: true, effort: "high", temperature: "0.5", verbose: false }, reason))
      .toEqual({ effort: "high", show_thinking: false });
    const plain = modelCaps("chatgpt", "gpt-4o", false);
    expect(buildOptions("chatgpt", { thinking: true, effort: "high", temperature: "0.5", verbose: false }, plain))
      .toEqual({ temperature: 0.5, show_thinking: false });
  });
  it("ollama sends thinking + temperature", () => {
    const caps = modelCaps("ollama", "llama3", false);
    expect(buildOptions("ollama", { thinking: false, effort: "high", temperature: "0.2", verbose: false }, caps))
      .toEqual({ thinking: false, temperature: 0.2, show_thinking: false });
  });
  it("gemini sends reasoning effort only, never temperature", () => {
    const caps = modelCaps("gemini", "gemini-3.5-flash", false);
    expect(buildOptions("gemini", { thinking: true, effort: "high", temperature: "0.3", verbose: false }, caps))
      .toEqual({ effort: "high", show_thinking: false });
  });
  it("kimi sends effort for k3 and the thinking flag for k2, never both", () => {
    // The two families take different API fields, so exactly one must be sent:
    // a thinking flag on K3 or an effort on K2 would be rejected upstream.
    const k3 = modelCaps("kimi", "kimi-k3", true);
    expect(buildOptions("kimi", { thinking: true, effort: "max", temperature: "0.5", verbose: false }, k3))
      .toEqual({ effort: "max", show_thinking: false });
    const k2 = modelCaps("kimi", "kimi-k2.6", true);
    expect(buildOptions("kimi", { thinking: true, effort: "max", temperature: "0.5", verbose: false }, k2))
      .toEqual({ thinking: true, show_thinking: false });
    const legacy = modelCaps("kimi", "moonshot-v1-128k", false);
    expect(buildOptions("kimi", { thinking: true, effort: "max", temperature: "0.5", verbose: false }, legacy))
      .toEqual({ temperature: 0.5, show_thinking: false });
  });
  it("meta sends reasoning effort only, never temperature", () => {
    const caps = modelCaps("meta", "muse-spark-1.1", true);
    expect(buildOptions("meta", { thinking: true, effort: "xhigh", temperature: "0.5", verbose: false }, caps))
      .toEqual({ effort: "xhigh", show_thinking: false });
  });
  it("grok: thinking on -> effort, no temp; off -> temperature", () => {
    const on = modelCaps("grok", "grok-4", true);
    expect(buildOptions("grok", { thinking: true, effort: "high", temperature: "0.5", verbose: false }, on))
      .toEqual({ thinking: true, effort: "high", show_thinking: false });
    const off = modelCaps("grok", "grok-4", false);
    expect(buildOptions("grok", { thinking: false, effort: "high", temperature: "0.5", verbose: false }, off))
      .toEqual({ thinking: false, temperature: 0.5, show_thinking: false });
  });
  it("minimax sends the thinking toggle only, never effort or temperature", () => {
    const caps = modelCaps("minimax", "MiniMax-M2", true);
    expect(buildOptions("minimax", { thinking: true, effort: "high", temperature: "0.5", verbose: true }, caps))
      .toEqual({ thinking: true, show_thinking: true });
  });
});

describe("agentCLI token formatting (fmtTokens)", () => {
  it("formats counts compactly across magnitudes", () => {
    // CLDR's short form, not the old hand-rolled ladder: the suffix is a
    // capital K in English, and in a locale that groups by another magnitude
    // the NUMBER changes too (see i18n_format.test.ts). Locale pinned here so
    // this asserts the format rather than the test runner's environment.
    setFormatLocale({}, "en-US");
    expect(fmtTokens(0)).toBe("0");
    expect(fmtTokens(812)).toBe("812");
    expect(fmtTokens(2680)).toBe("2.7K");
    expect(fmtTokens(48_200)).toBe("48.2K");
    expect(fmtTokens(482_000)).toBe("482K");
    expect(fmtTokens(1_234_000)).toBe("1.2M");
  });
});

describe("AgentCliWindow pop-out", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  it("keeps narrow mobile viewports in-app with reachable maximized controls", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );

    await waitFor(() => expect((screen.getByLabelText("Model") as HTMLSelectElement).value)
      .toBe("claude-opus-4-8"));
    expect(screen.getByRole("dialog", { name: "Agent Chat" })).toHaveClass("agentcli-maximized");
    expect(screen.queryByRole("button", { name: "Pop out to a separate window" })).toBeNull();
    expect(screen.getByRole("button", { name: "Options" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Minimize" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Close" })).toBeVisible();
  });

  it("keeps normal and minimized title bars below mobile browser chrome", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    const getComputedStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
      const style = getComputedStyle(element);
      Object.defineProperty(style, "scrollMarginTop", {
        configurable: true,
        value: "47px",
      });
      return style;
    });
    patchDurable({
      maximized: false,
      minimized: false,
      pos: { x: 20, y: 120 },
      pillPos: { x: -1, y: -1 },
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    const safeTop = 47;
    const chat = screen.getByRole("dialog", { name: "Agent Chat" });
    const titlebar = chat.querySelector(".agentcli-titlebar");
    if (!titlebar) throw new Error("Agent Chat title bar not found");

    fireEvent.pointerDown(titlebar, { clientX: 100, clientY: 120 });
    fireEvent.pointerMove(window, { clientX: 100, clientY: -500 });
    fireEvent.pointerUp(window);
    expect(chat.style.top).toBe(`${safeTop}px`);

    fireEvent.click(screen.getByRole("button", { name: "Minimize" }));
    await waitFor(() => expect(chat).toHaveClass("agentcli-minimized"));
    expect(chat.style.top).toBe(`${safeTop}px`);

    fireEvent.pointerDown(titlebar, { clientX: 100, clientY: safeTop });
    fireEvent.pointerMove(window, { clientX: 100, clientY: -500 });
    fireEvent.pointerUp(window);
    expect(chat.style.top).toBe(`${safeTop}px`);
  });

  it("moves the live chat into a minimal popup and back into Home Assistant", async () => {
    const { popup, iframe } = createPopupWindow();
    const open = vi.spyOn(window, "open").mockReturnValue(popup);
    const close = vi.spyOn(popup, "close");
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}}
                      hass={{ enableShortcuts: true, language: "zh-Hans" }} />,
    );

    const popOutButton = screen.getByRole("button", { name: "Pop out to a separate window" });
    const popOutIcon = popOutButton.querySelector("svg");
    expect(popOutIcon).toHaveAttribute("fill", "currentColor");
    expect(popOutIcon?.querySelector("path")).toHaveAttribute(
      "d",
      "M14 3V5H17.59L7.76 14.83L9.17 16.24L19 6.41V10H21V3M19 19H5V5H12V3H5C3.89 3 3 3.9 3 5V19A2 2 0 0 0 5 21H19A2 2 0 0 0 21 19V12H19V19Z",
    );
    const inAppGearPath = screen.getByRole("button", { name: "Options" })
      .querySelector("svg path")?.getAttribute("d");
    expect(inAppGearPath).toBeTruthy();
    fireEvent.click(popOutButton);

    expect(open).toHaveBeenCalledWith(
      "",
      "phoenix-mcp-agent-chat",
      expect.stringContaining("popup=yes"),
    );
    const popupUi = within(popup.document.body);
    expect(popup.document.documentElement.lang).toBe("zh-Hans");
    expect(popup.document.querySelector('meta[name="viewport"]')).toHaveAttribute(
      "content", "width=device-width, initial-scale=1",
    );
    expect(popupUi.getByRole("dialog", { name: "Agent Chat" })).toHaveClass("agentcli-popped-out");
    const popInButton = popupUi.getByRole("button", { name: "Pop back into Home Assistant" });
    expect(popInButton).toBeVisible();
    expect(popInButton.querySelector("svg")).toHaveAttribute("fill", "currentColor");
    expect(popInButton.querySelectorAll("svg path")).toHaveLength(2);
    const popOutGear = popupUi.getByRole("button", { name: "Options" });
    expect(popOutGear.querySelector("svg")).toHaveAttribute("viewBox", "0 0 24 24");
    expect(popOutGear.querySelector("svg path")?.getAttribute("d")).toBe(inAppGearPath);
    expect(popupUi.queryByRole("button", { name: "Minimize" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Agent Chat" })).toBeNull();

    expect(focusAgentCliPopup()).toBe(true);
    expect(popup.focus).toHaveBeenCalledTimes(2);

    fireEvent.click(popInButton);
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Agent Chat" })).toBeVisible());
    expect(close).toHaveBeenCalledOnce();
    expect(focusAgentCliPopup()).toBe(false);
    iframe.remove();
  });

  it("honors the profile-gated Shift+A toggle while the popup has focus", () => {
    const { popup, iframe } = createPopupWindow();
    vi.spyOn(window, "open").mockReturnValue(popup);
    const onClose = vi.fn();
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={onClose} hass={{ enableShortcuts: true }} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pop out to a separate window" }));

    const PopupKeyboardEvent = (popup as Window & { KeyboardEvent: typeof KeyboardEvent }).KeyboardEvent;
    popup.dispatchEvent(new PopupKeyboardEvent("keydown", {
      key: "A", code: "KeyA", shiftKey: true, bubbles: true,
    }));
    expect(onClose).toHaveBeenCalledOnce();
    iframe.remove();
  });

  it("reads the live shortcut profile setting in the popup", () => {
    const { popup, iframe } = createPopupWindow();
    vi.spyOn(window, "open").mockReturnValue(popup);
    const onClose = vi.fn();
    let shortcutsEnabled = false;
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={onClose}
                      getHass={() => ({ enableShortcuts: shortcutsEnabled })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pop out to a separate window" }));

    const PopupKeyboardEvent = (popup as Window & { KeyboardEvent: typeof KeyboardEvent }).KeyboardEvent;
    const pressShortcut = () => popup.dispatchEvent(new PopupKeyboardEvent("keydown", {
      key: "A", code: "KeyA", shiftKey: true, bubbles: true,
    }));
    pressShortcut();
    expect(onClose).not.toHaveBeenCalled();

    shortcutsEnabled = true;
    pressShortcut();
    expect(onClose).toHaveBeenCalledOnce();
    iframe.remove();
  });

  it("does not abort an in-flight turn while moving between hosts", async () => {
    const { popup, iframe } = createPopupWindow();
    vi.spyOn(window, "open").mockReturnValue(popup);
    let requestSignal: AbortSignal | undefined;
    agentCliChat.mockImplementation((...args: unknown[]) => {
      requestSignal = args[2] as AbortSignal;
      return new Promise(() => {});
    });
    const view = render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} hass={{ enableShortcuts: true }} />,
    );

    fireEvent.change(screen.getByRole("textbox", { name: "Message Agent Chat" }), {
      target: { value: "keep working" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(requestSignal).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Pop out to a separate window" }));
    expect(requestSignal?.aborted).toBe(false);
    fireEvent.click(within(popup.document.body).getByRole("button", {
      name: "Pop back into Home Assistant",
    }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Agent Chat" })).toBeVisible());
    expect(requestSignal?.aborted).toBe(false);

    view.unmount();
    iframe.remove();
  });

  it("shows recovery guidance when the browser blocks the popup", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} hass={{ enableShortcuts: true }} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Pop out to a separate window" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Your browser blocked the pop-out. Allow pop-ups for Home Assistant, then try again.",
    );
  });
});

describe("AgentCliWindow streaming", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    clearReasonDraft("ap9");
    clearReasonDraft("ap10");
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  it("streams a turn: user, assistant text, tool call/result, and approval flow", async () => {
    approveApproval.mockResolvedValue({});
    // Scripted event sequence delivered synchronously when a message is sent.
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("ready", { provider: "claude", model: "claude-opus-4-8" });
      onEvent("assistant_delta", { text: "Turning on " });
      onEvent("assistant_delta", { text: "the light." });
      onEvent("tool_call", { id: "tc1", name: "call_service", arguments: {} });
      onEvent("approval_required", { approval_id: "ap1", tool_name: "call_service", review_url: "/x" });
      onEvent("approval_resolved", { approval_id: "ap1", status: "approved" });
      onEvent("tool_result", { id: "tc1", name: "call_service", is_error: false, summary: "applied" });
      onEvent("messages", { messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "ok" }] });
      onEvent("done", { stop_reason: "end_turn" });
    });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "turn on the light" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(agentCliChat).toHaveBeenCalledTimes(1));
    // User bubble + streamed assistant text. The completed reply also appears
    // in the sr-only live announcer, hence getAllByText.
    expect(screen.getByText("turn on the light")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText(/Turning on the light\./).length).toBeGreaterThan(0));
    // The finished reply is announced to assistive tech via the status region.
    const announcer = document.querySelector(".agentcli-window .sr-only[role='status']");
    expect(announcer?.textContent).toBe("Turning on the light.");
    // The approval card resolved (buttons replaced by the status label, which
    // is the catalog's, never the server's raw slug).
    await waitFor(() => expect(screen.getByText("Approved")).toBeInTheDocument());
    // Verbose is off by default: the tool-result detail is hidden.
    expect(screen.queryByText(/applied/)).toBeNull();
  });

  it("pauses at the round-cap checkpoint and resumes with continue:true on Continue", async () => {
    // First turn ends with continue_required; second turn (Continue) must send
    // continue:true and no new user message, re-sending the held conversation.
    agentCliChat
      .mockImplementationOnce(async (_body, onEvent: (n: string, p: unknown) => void) => {
        onEvent("tool_call", { id: "tc1", name: "get_history", arguments: {} });
        onEvent("tool_result", { id: "tc1", name: "get_history", is_error: false, summary: "…" });
        onEvent("continue_required", { iterations: 20 });
        onEvent("messages", { messages: [{ role: "user", content: "why" }, { role: "assistant", content: "looking" }] });
        onEvent("done", { stop_reason: "tool_use" });
      })
      .mockImplementationOnce(async (_body, onEvent: (n: string, p: unknown) => void) => {
        onEvent("assistant_delta", { text: "It was the PIR automation." });
        onEvent("messages", { messages: [] });
        onEvent("done", { stop_reason: "end_turn" });
      });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "why did the light turn on" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    // The checkpoint control appears with the round count.
    const continueBtn = await screen.findByRole("button", { name: "Continue" });
    expect(screen.getByText(/Paused after 20 steps/)).toBeInTheDocument();

    fireEvent.click(continueBtn);

    await waitFor(() => expect(agentCliChat).toHaveBeenCalledTimes(2));
    const secondBody = agentCliChat.mock.calls[1][0] as Record<string, unknown>;
    expect(secondBody.continue).toBe(true);
    expect(secondBody.user).toBeUndefined();
    // The held conversation from the paused turn is re-sent.
    expect((secondBody.messages as unknown[]).length).toBe(2);
    // The resumed answer streams in, and the checkpoint control is gone.
    await waitFor(() => expect(screen.getAllByText(/It was the PIR automation\./).length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
  });

  it("shows the rejection reason and the approved-but-failed detail on resolved approvals", async () => {
    // A bare "rejected" hid WHY (live-found: an admin's Approve whose executor
    // failed read as a refusal); the resolved card must carry the reason.
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("approval_required", { approval_id: "ap1", tool_name: "add_dashboard_card", review_url: "/x" });
      onEvent("approval_resolved", { approval_id: "ap1", status: "rejected", reason: "wrong sensor, use the outdoor one" });
      onEvent("approval_required", { approval_id: "ap2", tool_name: "edit_dashboard_card", review_url: "/x" });
      onEvent("approval_resolved", { approval_id: "ap2", status: "execution_failed", reason: "This configuration changed since you last read it" });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "add the card" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText(/Rejected/)).toBeInTheDocument());
    expect(screen.getByText(/wrong sensor, use the outdoor one/)).toBeInTheDocument();
    // The same label also reaches the live region now that the announcement
    // resolves the status through the catalog, so scope this to the card.
    const statuses = Array.from(document.querySelectorAll(".agentcli-approval-status"));
    expect(statuses.map((n) => n.textContent).join(" ")).toContain("approved, but execution failed");
    expect(screen.getByText(/changed since you last read it/)).toBeInTheDocument();
  });

  it("shows tool activity only when verbose output is enabled", async () => {
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("tool_call", { id: "tc1", name: "get_overview", arguments: {} });
      onEvent("tool_result", { id: "tc1", name: "get_overview", is_error: false, summary: "90 entities" });
      onEvent("assistant_delta", { text: "You have 9 lights." });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    // Enable verbose via the gear popover.
    fireEvent.click(screen.getByLabelText("Options"));
    fireEvent.click(screen.getByLabelText("Show verbose output"));
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "how many lights?" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText(/You have 9 lights\./)).toBeInTheDocument());
    // With verbose on, the tool call + result detail are visible.
    expect(screen.getAllByText("get_overview").length).toBeGreaterThan(0);
    expect(screen.getByText(/90 entities/)).toBeInTheDocument();
  });

  it("token-usage footer accumulates across turns, resets on /clear, and hides via the gear toggle", async () => {
    // Turn 1: two usage events (turn-cumulative from the server); the footer
    // shows the newest totals plus the newest call's context size. Locale
    // pinned because the footer now renders CLDR's compact form, which differs
    // per language (48.2K in English, 4.8万 in Chinese).
    setFormatLocale({}, "en-US");
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("usage", { input_tokens: 1000, output_tokens: 50, context_tokens: 1000 });
      onEvent("assistant_delta", { text: "Hi." });
      onEvent("usage", { input_tokens: 2600, output_tokens: 80, context_tokens: 1600 });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    // Enabled by default, rendered even before any usage arrives.
    expect(screen.getByText(/Session 0 tokens/)).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/Session 2\.7K tokens · context 1\.6K/)).toBeInTheDocument());

    // Turn 2 adds onto the session base (2600 in + 80 out carried over).
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("usage", { input_tokens: 3000, output_tokens: 100, context_tokens: 3000 });
      onEvent("assistant_delta", { text: "Again." });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    fireEvent.change(textarea, { target: { value: "more" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    // 2680 + 3100 = 5780 total; context is the newest call's input.
    await waitFor(() => expect(screen.getByText(/Session 5\.8K tokens · context 3K/)).toBeInTheDocument());

    // /clear resets the counter with the conversation.
    fireEvent.change(textarea, { target: { value: "/clear" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/Session 0 tokens/)).toBeInTheDocument());

    // The gear toggle hides the footer and persists the choice.
    fireEvent.click(screen.getByLabelText("Options"));
    fireEvent.click(screen.getByLabelText("Show token usage"));
    expect(screen.queryByText(/Session 0 tokens/)).toBeNull();
    expect(getDurable().showUsage).toBe(false);
  });

  it("timestamps are off by default and appear on message bubbles via the gear toggle", async () => {
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("assistant_delta", { text: "Hello there." });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "hi" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getAllByText("Hello there.").length).toBeGreaterThan(0));
    // Default off: no timestamp elements anywhere.
    expect(document.querySelector(".agentcli-ts")).toBeNull();

    // Toggle on: both the user and assistant bubbles show a time, and the
    // choice persists.
    fireEvent.click(screen.getByLabelText("Options"));
    fireEvent.click(screen.getByLabelText("Show timestamps"));
    await waitFor(() => expect(document.querySelectorAll(".agentcli-ts").length).toBe(2));
    for (const el of document.querySelectorAll(".agentcli-ts")) {
      expect(el.textContent?.trim()).toBeTruthy();
    }
    expect(getDurable().showTimestamps).toBe(true);
  });

  it("token-usage footer says so when a provider reports no usage, and recovers when one does", async () => {
    // A completed turn with no usage events marks the footer (a forever-zero
    // counter would read as broken); a later turn that DOES report replaces it.
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("assistant_delta", { text: "Quiet provider." });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "hi" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("No usage data available from this provider")).toBeInTheDocument());

    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("usage", { input_tokens: 500, output_tokens: 20, context_tokens: 500 });
      onEvent("assistant_delta", { text: "Chatty provider." });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    fireEvent.change(textarea, { target: { value: "again" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/Session 520 tokens · context 500/)).toBeInTheDocument());
    expect(screen.queryByText("No usage data available from this provider")).toBeNull();
  });

  it("Cancel keeps the partial turn in the transcript instead of wiping it", async () => {
    // Live-found: a long agentic exchange is one turn, and Cancel dropped the
    // live entries, erasing the whole visible conversation. Cancel must fold
    // the partial turn into the transcript, flip any still-pending approval
    // card to cancelled (matching the server's orphan cleanup), and add a
    // Cancelled marker, while still restoring the query for editing.
    agentCliChat.mockImplementation((_body, onEvent: (n: string, p: unknown) => void, signal) => {
      onEvent("assistant_delta", { text: "Working on it." });
      onEvent("approval_required", { approval_id: "ap1", tool_name: "add_dashboard_card", review_url: "/x" });
      // Stay in flight until aborted, like a real streaming request.
      return new Promise((_resolve, reject) => {
        (signal as AbortSignal).addEventListener("abort",
          () => reject(new DOMException("Aborted", "AbortError")));
      });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "add a chart card" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("Working on it.")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // The partial turn stays visible with a Cancelled marker appended. The
    // user bubble is asserted via its class because the restored textarea
    // value also matches the raw text.
    expect(document.querySelector(".agentcli-msg-user")?.textContent).toBe("add a chart card");
    expect(screen.getByText("Working on it.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Cancelled.")).toBeInTheDocument());
    // The pending approval card resolved to cancelled: buttons gone.
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    // The query is restored to the input box for editing and resending.
    expect((textarea as HTMLTextAreaElement).value).toBe("add a chart card");
  });

  it("the /clear slash command starts a new conversation without calling the model", async () => {
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("assistant_delta", { text: "Hello there." });
      onEvent("messages", { messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "ok" }] });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "hi" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(agentCliChat).toHaveBeenCalledTimes(1));
    // Transcript + sr-only announcer both carry the finished reply.
    await waitFor(() => expect(screen.getAllByText("Hello there.").length).toBeGreaterThan(0));

    // /clear wipes the transcript (including the live announcer), does not call
    // the model, and is not itself rendered as a user message.
    fireEvent.change(textarea, { target: { value: "/clear" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(screen.queryByText("Hello there.")).toBeNull());
    expect(agentCliChat).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("/clear")).toBeNull();
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("Claude thinking dropdown folds on/off with effort and persists", async () => {
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText("Options"));
    // Single dropdown (no separate effort control); the popover closes with "Close".
    const sel = screen.getByLabelText("Thinking") as HTMLSelectElement;
    expect(sel.value).toBe("high");
    expect(screen.getByText("Close")).toBeInTheDocument();
    fireEvent.change(sel, { target: { value: "off" } });
    expect(getDurable().options.thinking).toBe(false);
    fireEvent.change(sel, { target: { value: "low" } });
    expect(getDurable().options).toMatchObject({ thinking: true, effort: "low" });
  });

  it("Approve button calls the approve endpoint", async () => {
    approveApproval.mockResolvedValue({});
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("approval_required", { approval_id: "ap9", tool_name: "restart_ha", review_url: "/x" });
      // Do not resolve; leave the buttons up so the test can click Approve.
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "tool_use" });
    });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "restart" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText("Approve")).toBeInTheDocument());
    // The card also offers a Review link to the full approval in the panel.
    expect(screen.getByText("Review…")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Approve"));
    expect(approveApproval).toHaveBeenCalledWith("ap9");
  });

  it("hides the buttons when the approval is acted on somewhere else", async () => {
    // The bug this closes: the bubble learned about a resolution ONLY from the
    // SSE stream, which rides the agent's turn resuming. Rejecting in the panel
    // left Approve and Reject sitting here until the model answered, so the
    // operator could click an action the server would only answer with a 409.
    // Nothing is streamed here after approval_required, which is the point:
    // the update has to arrive without the agent saying anything.
    const handlers: Record<string, (ev: unknown) => void> = {};
    const el = document.createElement("home-assistant");
    (el as unknown as { hass: unknown }).hass = {
      connection: {
        subscribeEvents: (cb: (ev: unknown) => void, event: string) => {
          handlers[event] = cb;
          return Promise.resolve(() => {});
        },
      },
    };
    document.body.appendChild(el);
    try {
      agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
        onEvent("approval_required", { approval_id: "ap9", tool_name: "restart_ha", review_url: "/x" });
        onEvent("messages", { messages: [] });
        onEvent("done", { stop_reason: "tool_use" });
      });

      render(
        <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                        initialTokenId="t1" onClose={() => {}} />,
      );
      await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
      const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
      fireEvent.change(textarea, { target: { value: "restart" } });
      fireEvent.keyDown(textarea, { key: "Enter" });
      await waitFor(() => expect(screen.getByText("Approve")).toBeInTheDocument());

      handlers["phoenix_mcp_approval_resolved"]({
        data: { approval_id: "ap9", status: "rejected" },
      });

      await waitFor(() => expect(screen.queryByText("Approve")).toBeNull());
      expect(rejectApproval).not.toHaveBeenCalled();
    } finally {
      document.body.removeChild(el);
    }
  });

  it("shows an approval claimed elsewhere as in progress, and restores it on release", async () => {
    // A claim is an admin's Approve executing its saved action inline, which has
    // no SSE frame at all. Release means that execution failed and the approval
    // is pending and actionable again, so the buttons have to come back.
    const handlers: Record<string, (ev: unknown) => void> = {};
    const el = document.createElement("home-assistant");
    (el as unknown as { hass: unknown }).hass = {
      connection: {
        subscribeEvents: (cb: (ev: unknown) => void, event: string) => {
          handlers[event] = cb;
          return Promise.resolve(() => {});
        },
      },
    };
    document.body.appendChild(el);
    try {
      agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
        onEvent("approval_required", { approval_id: "ap9", tool_name: "restart_ha", review_url: "/x" });
        onEvent("messages", { messages: [] });
        onEvent("done", { stop_reason: "tool_use" });
      });

      render(
        <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                        initialTokenId="t1" onClose={() => {}} />,
      );
      await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
      const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
      fireEvent.change(textarea, { target: { value: "restart" } });
      fireEvent.keyDown(textarea, { key: "Enter" });
      await waitFor(() => expect(screen.getByText("Approve")).toBeInTheDocument());

      handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: "ap9", claimed: true } });
      await waitFor(() => expect(screen.queryByText("Approve")).toBeNull());

      handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: "ap9", claimed: false } });
      await waitFor(() => expect(screen.getByText("Approve")).toBeInTheDocument());
    } finally {
      document.body.removeChild(el);
    }
  });

  it("Reject sends the reason drafted in the panel's approval modal, then clears it", async () => {
    rejectApproval.mockResolvedValue({});
    setReasonDraft("ap9", "put it on the Kitchen view instead");
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("approval_required", { approval_id: "ap9", tool_name: "add_dashboard_card", review_url: "/x" });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "tool_use" });
    });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "add a card" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText("Reject")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Reject"));
    await waitFor(() => expect(rejectApproval).toHaveBeenCalledWith(
      "ap9", { reason: "put it on the Kitchen view instead" }));
    // Consumed: a later rejection of another approval does not inherit it.
    await waitFor(() => expect(getReasonDraft("ap9")).toBe(""));
  });

  it("Reject with no drafted reason sends an empty body", async () => {
    rejectApproval.mockResolvedValue({});
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("approval_required", { approval_id: "ap10", tool_name: "add_dashboard_card", review_url: "/x" });
      onEvent("messages", { messages: [] });
      onEvent("done", { stop_reason: "tool_use" });
    });

    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "add a card" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText("Reject")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Reject"));
    await waitFor(() => expect(rejectApproval).toHaveBeenCalledWith("ap10", {}));
  });

  it("shows an empty-state prompt when no provider is configured", async () => {
    render(
      <AgentCliWindow tokens={TOKENS} instances={[]} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    expect(screen.getByText(/No provider account is configured/i)).toBeInTheDocument();
  });

  it("Cancel stops the turn, restores the query, and keeps the partial output", async () => {
    let signalRef: AbortSignal | undefined;
    agentCliChat.mockImplementation((_body, onEvent: (n: string, p: unknown) => void, signal: AbortSignal) => {
      signalRef = signal;
      onEvent("assistant_delta", { text: "half an answer" });
      // Hang until aborted (models can take many seconds; user hits Cancel).
      return new Promise((_res, rej) => {
        signal.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const ta = screen.getByPlaceholderText(/Message Agent Chat/i) as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "do the thing" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    await waitFor(() => expect(screen.getByText(/half an answer/)).toBeInTheDocument());
    fireEvent.click(screen.getByText("Cancel"));

    await waitFor(() => expect(screen.getByText("Send")).toBeInTheDocument());
    expect(signalRef?.aborted).toBe(true);
    expect(screen.getByText(/half an answer/)).toBeInTheDocument();  // partial kept in the transcript
    expect(ta.value).toBe("do the thing");                           // query restored for editing
  });

  it("persists selections and restores them on a fresh mount", async () => {
    getAgentCliModels.mockResolvedValue({ models: ["m-a", "m-b"] });
    const { unmount } = render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe("m-a"));
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "m-b" } });
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: "t2" } });
    fireEvent.click(screen.getByLabelText("Options"));
    fireEvent.click(screen.getByLabelText("Show verbose output"));

    await waitFor(() => expect(getDurable().model).toBe("m-b"));
    expect(getDurable().tokenId).toBe("t2");
    expect(getDurable().options.verbose).toBe(true);

    unmount();
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    // Restored from persistence, not reset to defaults.
    await waitFor(() => expect((screen.getByLabelText("Token") as HTMLSelectElement).value).toBe("t2"));
    expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe("m-b");
  });
});

describe("agentcli_state persistence", () => {
  beforeEach(() => __resetAgentCliState());
  it("round-trips durable fields through localStorage", () => {
    patchDurable({ open: true, model: "x", options: { thinking: false, effort: "low", temperature: "0.2", verbose: true } });
    const d = getDurable();
    expect(d.open).toBe(true);
    expect(d.model).toBe("x");
    expect(d.options.verbose).toBe(true);
    expect(d.options.effort).toBe("low");
  });
});

describe("agentCLI cancellation on close/unmount", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
    // A never-resolving promise keeps the request "in flight" so we can observe
    // whether its abort signal is tripped by Close / unmount.
    agentCliChat.mockReturnValue(new Promise(() => {}));
  });

  async function startTurn() {
    patchDurable({ open: true });
    const onClose = vi.fn();
    const utils = render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={onClose} />,
    );
    fireEvent.change(screen.getByPlaceholderText("Message Agent Chat…"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(agentCliChat).toHaveBeenCalled());
    const signal = agentCliChat.mock.calls[0][2] as AbortSignal;
    return { signal, onClose, utils };
  }

  it("Close aborts the in-flight request and calls onClose", async () => {
    const { signal, onClose } = await startTurn();
    expect(signal.aborted).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(signal.aborted).toBe(true);
    expect(onClose).toHaveBeenCalled();
  });

  it("unmount aborts the in-flight request", async () => {
    const { signal, utils } = await startTurn();
    expect(signal.aborted).toBe(false);
    utils.unmount();
    expect(signal.aborted).toBe(true);
  });
});

describe("unsent message box survives a panel remount", () => {
  // The panel-hosted window is unmounted whenever the user navigates away from
  // the Phoenix MCP panel in Home Assistant. The transcript survived that (it
  // lives in the session tier) but the half-written prompt did not, so a user
  // who typed, wandered off, and came back found the box emptied. The global
  // floating window never unmounts, so it never showed this.
  beforeEach(() => {
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
    patchDurable({ open: true, instanceId: "i-claude", model: "claude-opus-4-8" });
  });

  const box = () => screen.getByPlaceholderText(/message/i) as HTMLTextAreaElement;

  function mount() {
    return render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
  }

  it("restores the draft after unmount and remount", async () => {
    const first = mount();
    fireEvent.change(box(), { target: { value: "turn off the kitchen lights" } });
    await waitFor(() => expect(box().value).toBe("turn off the kitchen lights"));

    first.unmount();            // navigating away from the panel
    mount();                    // and back

    await waitFor(() => expect(box().value).toBe("turn off the kitchen lights"));
  });

  it("does not resurrect a draft that was cleared before leaving", async () => {
    const first = mount();
    fireEvent.change(box(), { target: { value: "half a thought" } });
    await waitFor(() => expect(box().value).toBe("half a thought"));
    fireEvent.change(box(), { target: { value: "" } });

    first.unmount();
    mount();

    await waitFor(() => expect(box().value).toBe(""));
  });

  it("the wizard prefill still wins over a stale draft", async () => {
    const first = mount();
    fireEvent.change(box(), { target: { value: "stale draft" } });
    await waitFor(() => expect(box().value).toBe("stale draft"));
    first.unmount();

    patchDurable({ prefill: "How many lights are in my home?" });
    mount();

    await waitFor(() => expect(box().value).toBe("How many lights are in my home?"));
  });
});

describe("AgentCliWindow held-tool progress", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  async function stream(events: Array<[string, unknown]>) {
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("ready", { provider: "claude", model: "claude-opus-4-8" });
      for (const [name, payload] of events) onEvent(name, payload);
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "build it" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(agentCliChat).toHaveBeenCalledTimes(1));
  }

  it("shows build progress even with verbose off", async () => {
    // Verbose is off by default, and that is exactly when this matters: without
    // it a multi-minute firmware build shows the operator nothing at all.
    await stream([
      ["tool_call", { id: "tc1", name: "wait_for_esphome_job", arguments: {} }],
      ["tool_progress", { id: "tc1", message: "Compiling rf-blaster2: 40%" }],
    ]);

    await waitFor(() =>
      expect(screen.getByText("Compiling rf-blaster2: 40%")).toBeInTheDocument());
    // The tool call itself stays hidden; only the progress line surfaces.
    expect(screen.queryByText(/calling/)).toBeNull();
  });

  it("replaces the line in place instead of stacking one per tick", async () => {
    await stream([
      ["tool_call", { id: "tc1", name: "wait_for_esphome_job", arguments: {} }],
      ["tool_progress", { id: "tc1", message: "Compiling rf-blaster2: 20%" }],
      ["tool_progress", { id: "tc1", message: "Compiling rf-blaster2: 80%" }],
      ["tool_progress", { id: "tc1", message: "Flashing rf-blaster2: 50%" }],
    ]);

    await waitFor(() =>
      expect(screen.getByText("Flashing rf-blaster2: 50%")).toBeInTheDocument());
    expect(screen.queryByText("Compiling rf-blaster2: 20%")).toBeNull();
    expect(screen.queryByText("Compiling rf-blaster2: 80%")).toBeNull();
    expect(document.querySelectorAll(".agentcli-progress").length).toBe(1);
  });

  it("keeps separate lines for separate calls", async () => {
    await stream([
      ["tool_progress", { id: "tc1", message: "Compiling a: 10%" }],
      ["tool_progress", { id: "tc2", message: "Compiling b: 10%" }],
    ]);

    await waitFor(() =>
      expect(document.querySelectorAll(".agentcli-progress").length).toBe(2));
  });
});

describe("AgentCliWindow generic activity line", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetAgentCliState();
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
  });

  // Held open, because the whole point of this line is what the operator sees
  // WHILE a turn runs. A completed stream would strip it before any assertion.
  async function running(events: Array<[string, unknown]>) {
    agentCliChat.mockImplementation((_body, onEvent: (n: string, p: unknown) => void, signal) => {
      onEvent("ready", { provider: "claude", model: "claude-opus-4-8" });
      for (const [name, payload] of events) onEvent(name, payload);
      return new Promise((_resolve, reject) => {
        (signal as AbortSignal).addEventListener("abort",
          () => reject(new DOMException("Aborted", "AbortError")));
      });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "do a few things" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(agentCliChat).toHaveBeenCalledTimes(1));
  }

  it("names the running tool with verbose off, where there is otherwise nothing", async () => {
    // The defect this closes: with verbose off the window rendered NOTHING for
    // the entire run, so an operator could not tell working from hung and read
    // the blank window as broken.
    await running([["tool_call", { id: "tc1", name: "get_states", arguments: {} }]]);

    await waitFor(() => expect(screen.getByText("Working: get_states")).toBeInTheDocument());
    expect(screen.queryByText(/calling/)).toBeNull();
  });

  it("moves to the newest call instead of stacking one line per tool", async () => {
    await running([
      ["tool_call", { id: "tc1", name: "get_states", arguments: {} }],
      ["tool_result", { id: "tc1", name: "get_states", is_error: false, summary: "ok" }],
      ["tool_call", { id: "tc2", name: "search_entities", arguments: {} }],
    ]);

    await waitFor(() => expect(screen.getByText("Working: search_entities")).toBeInTheDocument());
    expect(screen.queryByText("Working: get_states")).toBeNull();
    expect(document.querySelectorAll(".agentcli-progress").length).toBe(1);
  });

  it("yields to a tool that reports its own progress", async () => {
    // A build says "Compiling x: 40%", which is strictly more than "Working:",
    // so the two must never be stacked together.
    await running([
      ["tool_call", { id: "tc1", name: "wait_for_esphome_job", arguments: {} }],
      ["tool_progress", { id: "tc1", message: "Compiling rf-blaster2: 40%" }],
    ]);

    await waitFor(() =>
      expect(screen.getByText("Compiling rf-blaster2: 40%")).toBeInTheDocument());
    expect(screen.queryByText(/^Working: /)).toBeNull();
    expect(document.querySelectorAll(".agentcli-progress").length).toBe(1);
  });

  it("is gone once the turn finishes, leaving the reply and not a trail", async () => {
    // It is a live indicator, not history: the transcript should read as the
    // conversation, not as a log of how the answer was produced.
    agentCliChat.mockImplementation(async (_body, onEvent: (n: string, p: unknown) => void) => {
      onEvent("ready", { provider: "claude", model: "claude-opus-4-8" });
      onEvent("tool_call", { id: "tc1", name: "get_states", arguments: {} });
      onEvent("tool_result", { id: "tc1", name: "get_states", is_error: false, summary: "ok" });
      onEvent("assistant_delta", { text: "Three lights are on." });
      onEvent("done", { stop_reason: "end_turn" });
    });
    render(
      <AgentCliWindow tokens={TOKENS} instances={INSTANCES} scrollbackLines={500}
                      initialTokenId="t1" onClose={() => {}} />,
    );
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    const textarea = screen.getByPlaceholderText(/Message Agent Chat/i);
    fireEvent.change(textarea, { target: { value: "which lights are on" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(screen.getByText("Three lights are on.")).toBeInTheDocument());
    expect(screen.queryByText(/^Working: /)).toBeNull();
    expect(document.querySelectorAll(".agentcli-progress").length).toBe(0);
  });

  it("stays hidden with verbose on, where the calling lines already say it", async () => {
    await running([["tool_call", { id: "tc1", name: "get_states", arguments: {} }]]);
    await waitFor(() => expect(screen.getByText("Working: get_states")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Options" }));
    fireEvent.click(screen.getByLabelText("Show verbose output"));

    await waitFor(() => expect(screen.queryByText("Working: get_states")).toBeNull());
    expect(screen.getByText(/calling/)).toBeInTheDocument();
  });
});

// Declared capabilities only ever REMOVE a control, never add one. A provider
// saying "no reasoning" is authoritative; a provider saying nothing (which is
// most of them) leaves the shipped table standing. That asymmetry is what makes
// a failed or unsupported lookup degrade to today's behaviour instead of
// stripping a working model's controls.
describe("modelCaps with declared capabilities", () => {
  const vals2 = (c: { thinking: { value: string }[] }) => c.thinking.map((o) => o.value);

  it("removes the thinking control when the provider declares no reasoning", () => {
    const c = modelCaps("openrouter", "some/model", false, { thinking: false, tools: true });
    expect(c.thinking).toEqual([]);
  });

  it("removes temperature when the provider declares it unsupported", () => {
    const c = modelCaps("openrouter", "some/model", false, { temperature: false });
    expect(c.temperature).toBe(false);
  });

  it("leaves the shipped answer alone when nothing was declared", () => {
    const shipped = modelCaps("deepseek", "deepseek-v4-flash", false);
    const withEmpty = modelCaps("deepseek", "deepseek-v4-flash", false, {});
    expect(vals2(withEmpty)).toEqual(vals2(shipped));
    expect(withEmpty.temperature).toBe(shipped.temperature);
  });

  it("a declared TRUE never adds a control the table does not offer", () => {
    // Declaring support is not the same as knowing the levels: OpenRouter says
    // "reasoning", not which efforts. Adding a control from a bare boolean would
    // offer levels the model may reject.
    const shipped = modelCaps("claude", "claude-opus-4-8", false);
    const declared = modelCaps("claude", "claude-opus-4-8", false, { thinking: true, temperature: true });
    expect(vals2(declared)).toEqual(vals2(shipped));
    expect(declared.temperature).toBe(shipped.temperature);
  });

  it("an undeclared field is not a limit", () => {
    // Ollama omits temperature entirely because it takes one for every model.
    const shipped = modelCaps("ollama", "llama3", false);
    const declared = modelCaps("ollama", "llama3", false, { tools: true, thinking: true });
    expect(declared.temperature).toBe(shipped.temperature);
  });
});

// Probed levels are the ONE case where discovery may add rather than only remove,
// and it is sound only because the probe proved the field was validated first.
describe("modelCaps with probed effort levels", () => {
  const vals3 = (c: { thinking: { value: string }[] }) => c.thinking.map((o) => o.value);

  it("replaces the guessed level set with what the API accepted", () => {
    const c = modelCaps("deepseek", "deepseek-v4-flash", true, { effort_levels: ["low", "max"] });
    expect(vals3(c)).toEqual(["off", "low", "max"]);
  });

  it("keeps off, which is Phoenix not sending the field rather than a level", () => {
    const c = modelCaps("deepseek", "deepseek-v4-flash", true, { effort_levels: ["high"] });
    expect(vals3(c)[0]).toEqual("off");
  });

  it("moves the default when the probe refused it", () => {
    const c = modelCaps("deepseek", "deepseek-v4-flash", true, { effort_levels: ["low"] });
    expect(c.defaultLevel).toBe("low");
  });

  it("an empty list changes nothing, because it means unknown and not none", () => {
    const shipped = modelCaps("deepseek", "deepseek-v4-flash", true);
    const probed = modelCaps("deepseek", "deepseek-v4-flash", true, { effort_levels: [] });
    expect(vals3(probed)).toEqual(vals3(shipped));
  });

  it("does not invent a control on a model that has none", () => {
    const c = modelCaps("ollama", "llama3", false, { effort_levels: ["low", "high"] });
    const shipped = modelCaps("ollama", "llama3", false);
    expect(vals3(c)).toEqual(vals3(shipped));
  });
});

// The aggregator case: one key fronting many vendors, where no built-in answer
// fits and a reasoning model was therefore losing its reasoning entirely.
describe("modelCaps for aggregator providers", () => {
  const vals4 = (c: { thinking: { value: string }[] }) => c.thinking.map((o) => o.value);

  it.each(["openrouter", "nvidia"] as const)("%s starts with no thinking control", (kind) => {
    expect(modelCaps(kind, "vendor/model", false).thinking).toEqual([]);
  });

  it.each(["openrouter", "nvidia"] as const)("probed levels CREATE the control on %s", (kind) => {
    const c = modelCaps(kind, "vendor/reasoner", false, { effort_levels: ["low", "high"] });
    // "off" is added because a created control the operator cannot decline is
    // worse than no control at all.
    expect(vals4(c)).toEqual(["off", "low", "high"]);
    expect(c.defaultLevel).toBe("high");
  });

  it("a created control still defaults to a level the probe accepted", () => {
    const c = modelCaps("openrouter", "vendor/m", false, { effort_levels: ["low"] });
    expect(c.defaultLevel).toBe("low");
  });

  it("no probed levels means no control, not an empty dropdown", () => {
    // A model the aggregator does not validate establishes nothing, and the
    // starting point stands.
    expect(modelCaps("openrouter", "vendor/m", false, { temperature: true }).thinking).toEqual([]);
  });

  it("a boolean backend is still never given levels", () => {
    // Ollama normalizes reasoning to one flag across every model, so there is no
    // level vocabulary to discover and none may be invented.
    const c = modelCaps("ollama", "deepseek-r1:8b", false, { effort_levels: ["low", "high"] });
    expect(vals4(c)).toEqual(["off", "on"]);
  });
});
