import { describe, it, expect, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { BeforeAfter, RemovedPane } from "../components/DiffView";

// The approval diff renders a red/green line diff of the before/after configs
// (JSON converted to YAML), with a side-by-side <-> stacked layout toggle.
describe("BeforeAfter (approval diff)", () => {
  // The layout choice is persisted; clear it so each test starts side-by-side.
  beforeEach(() => localStorage.clear());
  it("tints changed lines red on the before side and green on the after side", () => {
    const { container } = render(
      <BeforeAfter
        before={JSON.stringify({ alias: "Morning", mode: "single" })}
        after={JSON.stringify({ alias: "Evening", mode: "single" })}
      />,
    );
    const panes = container.querySelectorAll("pre.raw-diff");
    expect(panes.length).toBe(2);
    expect(container.querySelector(".diff-remove")?.textContent).toContain("alias: Morning");
    expect(container.querySelector(".diff-add")?.textContent).toContain("alias: Evening");
    // The unchanged "mode" line carries no tone.
    const modeLine = Array.from(container.querySelectorAll(".diff-line"))
      .find((el) => el.textContent?.includes("mode: single"));
    expect(modeLine?.className).toBe("diff-line");
  });

  it("toggles between side-by-side and stacked layout", () => {
    const { container, getByRole } = render(
      <BeforeAfter before={JSON.stringify({ a: 1 })} after={JSON.stringify({ a: 2 })} />,
    );
    expect(container.querySelector(".yaml-diff-cols.stacked")).toBeNull();
    fireEvent.click(getByRole("button", { name: /stacked view/i }));
    expect(container.querySelector(".yaml-diff-cols.stacked")).toBeTruthy();
  });

  it("remembers the layout choice: a fresh diff opens stacked after choosing stacked", () => {
    const first = render(
      <BeforeAfter before={JSON.stringify({ a: 1 })} after={JSON.stringify({ a: 2 })} />,
    );
    fireEvent.click(first.getByRole("button", { name: /stacked view/i }));
    first.unmount();
    const second = render(
      <BeforeAfter before={JSON.stringify({ b: 1 })} after={JSON.stringify({ b: 2 })} />,
    );
    expect(second.container.querySelector(".yaml-diff-cols.stacked")).toBeTruthy();
  });

  it("shows an empty before pane for a create (null before) and all-added after", () => {
    const { container } = render(<BeforeAfter before={null} after={JSON.stringify({ x: 1 })} />);
    expect(container.querySelector(".yaml-pre-empty")?.textContent).toBe("(empty)");
    expect(container.querySelector(".diff-add")?.textContent).toContain("x: 1");
  });
});

// The code view hands both panes to HA's own <ha-code-editor> (syntax colours
// and a line-number gutter). In jsdom that element is never registered, so
// YamlView renders its <pre> fallback; the load-bearing difference either way is
// that the per-line diff markup is gone, which is what the hint has to admit.
describe("BeforeAfter code view", () => {
  beforeEach(() => localStorage.clear());

  const props = {
    before: JSON.stringify({ alias: "Morning", mode: "single" }),
    after: JSON.stringify({ alias: "Evening", mode: "single" }),
  };

  it("defaults to the line diff, not the code view", () => {
    const { container } = render(<BeforeAfter {...props} />);
    expect(container.querySelectorAll(".diff-line").length).toBeGreaterThan(0);
    expect(container.querySelector(".change-diff-hint")?.textContent)
      .toContain("tinted red");
  });

  it("switches both panes out of the line diff and still shows both sides", () => {
    const { container, getByRole } = render(<BeforeAfter {...props} />);
    fireEvent.click(getByRole("button", { name: /code editor view/i }));

    expect(container.querySelectorAll(".diff-line").length).toBe(0);
    expect(container.querySelectorAll("pre.raw-diff").length).toBe(0);
    const text = container.textContent ?? "";
    expect(text).toContain("alias: Morning");
    expect(text).toContain("alias: Evening");
  });

  it("admits that changed lines are no longer marked", () => {
    // A syntax-coloured pane looks authoritative, so losing the red/green
    // tinting has to be stated or an approver will not notice it is gone.
    const { container, getByRole } = render(<BeforeAfter {...props} />);
    fireEvent.click(getByRole("button", { name: /code editor view/i }));
    const hint = container.querySelector(".change-diff-hint")?.textContent ?? "";
    expect(hint).toContain("line numbers");
    expect(hint).toContain("not marked");
  });

  it("toggles back to the line diff", () => {
    const { container, getByRole } = render(<BeforeAfter {...props} />);
    fireEvent.click(getByRole("button", { name: /code editor view/i }));
    fireEvent.click(getByRole("button", { name: /plain line diff/i }));
    expect(container.querySelectorAll(".diff-line").length).toBeGreaterThan(0);
  });

  it("remembers the choice, independently of the layout preference", () => {
    const first = render(<BeforeAfter {...props} />);
    fireEvent.click(first.getByRole("button", { name: /code editor view/i }));
    first.unmount();

    const second = render(<BeforeAfter {...props} />);
    expect(second.container.querySelectorAll(".diff-line").length).toBe(0);
    // The layout key is untouched: still side-by-side.
    expect(second.container.querySelector(".yaml-diff-cols.stacked")).toBeNull();
  });

  it("composes with the toolbar control the dashboard surfaces already put here", () => {
    // Both must coexist: this is why it is an icon toggle rather than a second
    // segmented control whose first segment would also read "Diff".
    const { getByRole, getByTestId } = render(
      <BeforeAfter {...props} toolbarExtra={<span data-testid="extra">Diff|Preview</span>} />,
    );
    expect(getByTestId("extra")).toBeTruthy();
    expect(getByRole("button", { name: /code editor view/i })).toBeTruthy();
  });
});

describe("RemovedPane (delete approval)", () => {
  it("renders every line of the removed config in red", () => {
    const { container } = render(<RemovedPane value={JSON.stringify({ alias: "Gone", mode: "single" })} />);
    const removed = container.querySelectorAll(".diff-remove");
    expect(removed.length).toBeGreaterThanOrEqual(2);
    const text = Array.from(removed).map((el) => el.textContent).join("\n");
    expect(text).toContain("alias: Gone");
    expect(text).toContain("mode: single");
    // Nothing is added.
    expect(container.querySelector(".diff-add")).toBeNull();
  });
});
