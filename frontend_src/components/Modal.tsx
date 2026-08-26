/**Accessible modal with focus trap, ARIA dialog role, and Escape to close.*/
import React, { useEffect, useRef, useCallback } from "react";

interface Props {
  titleId: string;
  onClose?: () => void;
  /** Move through the ordered records behind this dialog. Omit at a boundary. */
  onNavigatePrevious?: () => void;
  onNavigateNext?: () => void;
  /** Consume vertical arrows even at list boundaries, preventing page scroll. */
  recordNavigation?: boolean;
  /** Widen the dialog (e.g. for side-by-side diffs that need more room). */
  wide?: boolean;
  children: React.ReactNode;
}

// A stack of open modals so Escape only closes the topmost one (nested dialogs
// like the discard-confirm prompt must not also dismiss the editor behind them).
// Module-scoped per bundle; the panel and the injector never share a render tree.
const modalStack: Array<() => void> = [];

const VERTICAL_ARROW_OWNER = [
  "input",
  "textarea",
  "select",
  "[contenteditable='true']",
  "ha-code-editor",
  ".cm-editor",
  "[role='listbox']",
  "[role='menu']",
  "[role='tree']",
  "[role='grid']",
  "[role='slider']",
  "[role='spinbutton']",
].join(", ");

function ownsVerticalArrows(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest(VERTICAL_ARROW_OWNER));
}

/** Find a scroll region inside this modal that can consume the current finger
 * movement. composedPath includes editors' shadow-DOM scrollers, which a plain
 * closest() walk cannot see. Never search past the modal into the page below. */
function touchScrollOwner(
  event: TouchEvent,
  modal: HTMLElement,
  backdrop: HTMLElement,
  deltaY: number,
): HTMLElement | null {
  for (const node of event.composedPath()) {
    if (node instanceof HTMLElement) {
      const maxScroll = node.scrollHeight - node.clientHeight;
      const scrollableOverflow = node === modal
        || /^(auto|scroll|overlay)$/.test(window.getComputedStyle(node).overflowY);
      if (scrollableOverflow && maxScroll > 0) {
        const canScroll = deltaY > 0
          ? node.scrollTop < maxScroll
          : deltaY < 0 && node.scrollTop > 0;
        if (canScroll) return node;
      }
    }
    if (node === modal || node === backdrop) break;
  }
  return null;
}

export function Modal({ titleId, onClose, onNavigatePrevious, onNavigateNext, recordNavigation, wide, children }: Props) {
  const backdropRef = useRef<HTMLDivElement>(null);
  const modalRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const onNavigatePreviousRef = useRef(onNavigatePrevious);
  const onNavigateNextRef = useRef(onNavigateNext);
  const recordNavigationRef = useRef(recordNavigation);
  onCloseRef.current = onClose;
  onNavigatePreviousRef.current = onNavigatePrevious;
  onNavigateNextRef.current = onNavigateNext;
  recordNavigationRef.current = recordNavigation;

  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null;
    const first = modalRef.current?.querySelector<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
    );
    // Fall back to the dialog itself (tabIndex={-1}) so focus never stays
    // behind the backdrop when a modal has no focusable child (e.g. loading).
    (first ?? modalRef.current)?.focus();
    return () => { previousFocus.current?.focus(); };
  }, []);

  // A fixed backdrop does not automatically own a touch scroll gesture. On
  // mobile, swipes from an empty dialog area—or outward at a scroll boundary—
  // can otherwise chain into the Phoenix panel underneath. Let a modal or
  // nested editor scroll natively while it has room; consume only the gesture
  // the moment nothing inside the dialog can move in that direction.
  useEffect(() => {
    const backdrop = backdropRef.current;
    const modal = modalRef.current;
    if (!backdrop || !modal) return;
    let lastY: number | null = null;
    const start = (event: TouchEvent) => {
      event.stopPropagation();
      lastY = event.touches.length === 1 ? event.touches[0].clientY : null;
    };
    const move = (event: TouchEvent) => {
      event.stopPropagation();
      if (lastY === null || event.touches.length !== 1) return;
      const nextY = event.touches[0].clientY;
      const deltaY = lastY - nextY;
      lastY = nextY;
      if (touchScrollOwner(event, modal, backdrop, deltaY)) return;
      if (event.cancelable) event.preventDefault();
    };
    const end = (event: TouchEvent) => {
      event.stopPropagation();
      lastY = null;
    };
    backdrop.addEventListener("touchstart", start, { passive: true });
    backdrop.addEventListener("touchmove", move, { passive: false });
    backdrop.addEventListener("touchend", end, { passive: true });
    backdrop.addEventListener("touchcancel", end, { passive: true });
    return () => {
      backdrop.removeEventListener("touchstart", start);
      backdrop.removeEventListener("touchmove", move);
      backdrop.removeEventListener("touchend", end);
      backdrop.removeEventListener("touchcancel", end);
    };
  }, []);

  // Escape is bound on the document, not the modal div, so it fires even when
  // focus has fallen outside the dialog (e.g. after clicking a non-focusable
  // label, which lands focus on <body>). Keyboard events are composed, so this
  // also catches Escape from inside the injector's shadow-root modal.
  useEffect(() => {
    const close = () => onCloseRef.current?.();
    modalStack.push(close);
    const onKey = (e: KeyboardEvent) => {
      if (modalStack[modalStack.length - 1] !== close) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        return;
      }
      if (
        (e.key !== "ArrowUp" && e.key !== "ArrowDown")
        || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing
        || ownsVerticalArrows(e.target)
      ) return;
      if (!recordNavigationRef.current) return;
      // Consume the key before resolving a direction. At the first/last item,
      // doing nothing is intentional; allowing the browser default here scrolls
      // the page behind the modal and makes the dialog appear to drift.
      e.preventDefault();
      e.stopPropagation();
      const navigate = e.key === "ArrowUp"
        ? onNavigatePreviousRef.current
        : onNavigateNextRef.current;
      if (!navigate) return;
      navigate();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const i = modalStack.lastIndexOf(close);
      if (i !== -1) modalStack.splice(i, 1);
    };
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const modal = modalRef.current;
    if (!modal) return;
    const focusable = modal.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
    );
    if (focusable.length === 0) {
      // Nothing tabbable inside: keep focus pinned on the dialog itself.
      e.preventDefault();
      modal.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }, []);

  return (
    <div ref={backdropRef} className="modal-backdrop" onClick={onClose}>
      <div
        ref={modalRef}
        className={`modal${wide ? " modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        {children}
      </div>
    </div>
  );
}
