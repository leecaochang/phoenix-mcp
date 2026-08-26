import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import { Modal } from "../components/Modal";

describe("Modal Escape handling", () => {
  it("closes on Escape even when focus is on document.body", () => {
    const onClose = vi.fn();
    render(
      <Modal titleId="t" onClose={onClose}>
        <p>body content with no focusable element</p>
      </Modal>
    );
    // Simulate focus having fallen outside the dialog (e.g. clicking a label).
    (document.activeElement as HTMLElement | null)?.blur?.();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Escape closes only the topmost modal when dialogs are nested", () => {
    const onCloseOuter = vi.fn();
    const onCloseInner = vi.fn();
    const { rerender } = render(
      <>
        <Modal titleId="outer" onClose={onCloseOuter}><p>outer</p></Modal>
        <Modal titleId="inner" onClose={onCloseInner}><p>inner</p></Modal>
      </>
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCloseInner).toHaveBeenCalledTimes(1);
    expect(onCloseOuter).not.toHaveBeenCalled();

    // The inner dialog closes (unmounts); Escape now reaches the outer one.
    rerender(
      <>
        <Modal titleId="outer" onClose={onCloseOuter}><p>outer</p></Modal>
      </>
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCloseOuter).toHaveBeenCalledTimes(1);
  });
});

describe("Modal focus handling", () => {
  it("focuses the dialog itself when no focusable child exists", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <p>loading, nothing focusable</p>
      </Modal>
    );
    const dialog = document.querySelector<HTMLElement>("[role='dialog']")!;
    expect(dialog.tabIndex).toBe(-1);
    expect(document.activeElement).toBe(dialog);
  });

  it("keeps Tab pinned inside an empty dialog instead of escaping", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <p>nothing focusable</p>
      </Modal>
    );
    const dialog = document.querySelector<HTMLElement>("[role='dialog']")!;
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(dialog);
  });

  it("focuses the first enabled control on mount, skipping disabled ones", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <button disabled>disabled first</button>
        <button>real first</button>
      </Modal>
    );
    expect((document.activeElement as HTMLElement).textContent).toBe("real first");
  });
});

describe("Modal touch containment", () => {
  it("consumes a swipe when no region inside the dialog can scroll", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <p>short modal</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    const leaked = vi.fn();
    document.body.addEventListener("touchmove", leaked);
    try {
      fireEvent.touchStart(dialog, { touches: [{ clientY: 220 }] });
      const dispatched = fireEvent.touchMove(dialog, {
        touches: [{ clientY: 170 }],
        cancelable: true,
      });
      expect(dispatched).toBe(false);
      expect(leaked).not.toHaveBeenCalled();
    } finally {
      document.body.removeEventListener("touchmove", leaked);
    }
  });

  it("consumes a swipe on the backdrop without inspecting the page scroller", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <p>short modal</p>
      </Modal>,
    );
    const backdrop = document.querySelector<HTMLElement>(".modal-backdrop")!;
    Object.defineProperty(document.body, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(document.body, "clientHeight", { configurable: true, value: 500 });
    document.body.scrollTop = 100;

    fireEvent.touchStart(backdrop, { touches: [{ clientY: 220 }] });
    expect(fireEvent.touchMove(backdrop, {
      touches: [{ clientY: 170 }],
      cancelable: true,
    })).toBe(false);
  });

  it("keeps native modal scrolling until the gesture reaches its boundary", () => {
    render(
      <Modal titleId="t" onClose={() => {}}>
        <p>long modal</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    Object.defineProperty(dialog, "scrollHeight", { configurable: true, value: 400 });
    Object.defineProperty(dialog, "clientHeight", { configurable: true, value: 100 });
    dialog.scrollTop = 100;

    fireEvent.touchStart(dialog, { touches: [{ clientY: 220 }] });
    expect(fireEvent.touchMove(dialog, {
      touches: [{ clientY: 170 }],
      cancelable: true,
    })).toBe(true);

    dialog.scrollTop = 300;
    fireEvent.touchStart(dialog, { touches: [{ clientY: 220 }] });
    expect(fireEvent.touchMove(dialog, {
      touches: [{ clientY: 170 }],
      cancelable: true,
    })).toBe(false);
  });
});

describe("Modal record navigation", () => {
  it("uses Up and Down only when that direction is available", () => {
    const previous = vi.fn();
    const next = vi.fn();
    const { rerender } = render(
      <Modal titleId="t" onNavigatePrevious={previous} onNavigateNext={next} recordNavigation>
        <button>first control</button>
      </Modal>,
    );

    fireEvent.keyDown(document, { key: "ArrowUp" });
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(previous).toHaveBeenCalledTimes(1);
    expect(next).toHaveBeenCalledTimes(1);

    rerender(
      <Modal titleId="t" onNavigateNext={next} recordNavigation>
        <button>first control</button>
      </Modal>,
    );
    expect(fireEvent.keyDown(document, { key: "ArrowUp" })).toBe(false);
    expect(previous).toHaveBeenCalledTimes(1);
  });

  it("leaves vertical arrow keys to editable controls", () => {
    const previous = vi.fn();
    const next = vi.fn();
    render(
      <Modal titleId="t" onNavigatePrevious={previous} onNavigateNext={next} recordNavigation>
        <input aria-label="reason" />
      </Modal>,
    );
    const input = document.querySelector("input")!;
    expect(fireEvent.keyDown(input, { key: "ArrowUp" })).toBe(true);
    expect(fireEvent.keyDown(input, { key: "ArrowDown" })).toBe(true);
    expect(previous).not.toHaveBeenCalled();
    expect(next).not.toHaveBeenCalled();
  });

  it("navigates only the topmost nested modal", () => {
    const outer = vi.fn();
    const inner = vi.fn();
    render(
      <>
        <Modal titleId="outer" onNavigateNext={outer} recordNavigation><p>outer</p></Modal>
        <Modal titleId="inner" onNavigateNext={inner} recordNavigation><p>inner</p></Modal>
      </>,
    );
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();
  });
});
