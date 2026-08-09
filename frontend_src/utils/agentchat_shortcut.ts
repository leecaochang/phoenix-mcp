/** Home Assistant-style keyboard shortcut handling for Agent Chat. */

interface ShortcutHass {
  enableShortcuts?: boolean;
}

function acceptsAlphanumericShortcut(event: KeyboardEvent, targetWindow: Window): boolean {
  const path = event.composedPath();
  // Elements created in the pop-out belong to a different JavaScript realm,
  // so `instanceof HTMLElement` from the opener rejects them. Use the target
  // window's constructor to keep the input guards identical in both windows.
  const HTMLElementCtor = (targetWindow as Window & { HTMLElement: typeof HTMLElement }).HTMLElement;
  const elements = path.filter(
    (target): target is HTMLElement => target instanceof HTMLElementCtor,
  );

  if (elements.some((el) => el.tagName === "HA-MENU" || el.tagName === "HA-CODE-EDITOR")) {
    return false;
  }

  const target = elements[0];
  if (!target) return true;
  if (target.isContentEditable || target.closest("[contenteditable=''], [contenteditable='true']")) {
    return false;
  }
  if (
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.parentElement?.tagName === "HA-SELECT" ||
    target.parentElement?.tagName === "HA-DROPDOWN"
  ) {
    return false;
  }
  if (target.tagName !== "INPUT") return true;

  return ["button", "checkbox", "hidden", "radio", "range"].includes(
    (target as HTMLInputElement).type,
  );
}

export function isAgentChatShortcut(
  event: KeyboardEvent,
  hass: ShortcutHass | null,
  targetWindow: Window = window,
): boolean {
  if (
    !hass?.enableShortcuts ||
    event.defaultPrevented ||
    event.repeat ||
    !event.shiftKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    (event.key.toLowerCase() !== "a" && event.code !== "KeyA") ||
    !acceptsAlphanumericShortcut(event, targetWindow) ||
    targetWindow.getSelection()?.toString()
  ) {
    return false;
  }
  return true;
}

export function registerAgentChatShortcut(
  getHass: () => ShortcutHass | null,
  action: () => void | Promise<void>,
  targetWindow: Window = window,
): () => void {
  const handler = (event: KeyboardEvent) => {
    if (!isAgentChatShortcut(event, getHass(), targetWindow)) return;
    event.preventDefault();
    void action();
  };
  targetWindow.addEventListener("keydown", handler);
  return () => targetWindow.removeEventListener("keydown", handler);
}
