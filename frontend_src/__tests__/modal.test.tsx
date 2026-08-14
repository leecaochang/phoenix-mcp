import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
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
