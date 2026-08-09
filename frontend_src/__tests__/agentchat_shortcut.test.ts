/** Agent Chat follows Home Assistant's keyboard-shortcut profile setting. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { registerAgentChatShortcut } from "../utils/agentchat_shortcut";

function pressShiftA(target: EventTarget = window, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "A",
    code: "KeyA",
    shiftKey: true,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

describe("Agent Chat Shift+A shortcut", () => {
  let dispose: (() => void) | undefined;

  afterEach(() => {
    dispose?.();
    dispose = undefined;
    vi.restoreAllMocks();
    document.body.replaceChildren();
  });

  it("invokes the show-or-hide action and consumes each Shift+A", () => {
    let visible = false;
    const toggle = vi.fn(() => { visible = !visible; });
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: true }), toggle);

    const showEvent = pressShiftA();
    expect(visible).toBe(true);
    const hideEvent = pressShiftA();

    expect(toggle).toHaveBeenCalledTimes(2);
    expect(visible).toBe(false);
    expect(showEvent.defaultPrevented).toBe(true);
    expect(hideEvent.defaultPrevented).toBe(true);
  });

  it("does nothing when the profile has keyboard shortcuts disabled", () => {
    const open = vi.fn();
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: false }), open);

    const event = pressShiftA();

    expect(open).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("ignores key-repeat events from holding Shift+A", () => {
    const toggle = vi.fn();
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: true }), toggle);

    const event = pressShiftA(window, { repeat: true });

    expect(toggle).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("does not turn typing a capital A into an open command", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    const open = vi.fn();
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: true }), open);

    const event = pressShiftA(input);

    expect(open).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("ignores selected text, other modifiers, and already handled events", () => {
    vi.spyOn(window, "getSelection").mockReturnValue({ toString: () => "selected" } as Selection);
    const open = vi.fn();
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: true }), open);
    pressShiftA();
    vi.mocked(window.getSelection).mockReturnValue(null);
    pressShiftA(window, { ctrlKey: true });
    const handled = new KeyboardEvent("keydown", {
      key: "A", code: "KeyA", shiftKey: true, bubbles: true, cancelable: true,
    });
    handled.preventDefault();
    window.dispatchEvent(handled);

    expect(open).not.toHaveBeenCalled();
  });

  it("removes the listener through its disposer", () => {
    const open = vi.fn();
    dispose = registerAgentChatShortcut(() => ({ enableShortcuts: true }), open);
    dispose();
    dispose = undefined;

    pressShiftA();

    expect(open).not.toHaveBeenCalled();
  });
});
