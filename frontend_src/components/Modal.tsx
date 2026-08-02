/**Accessible modal with focus trap, ARIA dialog role, and Escape to close.*/
import React, { useEffect, useRef, useCallback } from "react";

interface Props {
  titleId: string;
  onClose?: () => void;
  /** Widen the dialog (e.g. for side-by-side diffs that need more room). */
  wide?: boolean;
  children: React.ReactNode;
}

// A stack of open modals so Escape only closes the topmost one (nested dialogs
// like the discard-confirm prompt must not also dismiss the editor behind them).
// Module-scoped per bundle; the panel and the injector never share a render tree.
const modalStack: Array<() => void> = [];

export function Modal({ titleId, onClose, wide, children }: Props) {
  const modalRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

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

  // Escape is bound on the document, not the modal div, so it fires even when
  // focus has fallen outside the dialog (e.g. after clicking a non-focusable
  // label, which lands focus on <body>). Keyboard events are composed, so this
  // also catches Escape from inside the injector's shadow-root modal.
  useEffect(() => {
    const close = () => onCloseRef.current?.();
    modalStack.push(close);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && modalStack[modalStack.length - 1] === close) {
        e.stopPropagation();
        close();
      }
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
    <div className="modal-backdrop" onClick={onClose}>
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
