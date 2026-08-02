import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { MesaSuggestions } from "../components/MesaSuggestions";
import type { MesaSuggestion } from "../types";

const lockRow: MesaSuggestion = {
  key: "naked_risky:entity:lock.front",
  signal: "naked_risky",
  scope: "entity",
  subject_id: "lock.front",
  suggested_mode: "prohibited",
  reason: "No MESA profile covers this entity at any level, and it controls a physical security boundary.",
  evidence: { domain: "lock" },
};

const domainRow: MesaSuggestion = {
  key: "naked_risky:domain:update",
  signal: "naked_risky",
  scope: "domain",
  subject_id: "update",
  suggested_mode: "confirm",
  reason: "6 update entities have no MESA profile at any level.",
  evidence: { domain: "update", uncovered_count: 6, examples: [] },
};

const noop = () => {};
const baseProps = {
  dismissedCount: 0,
  busyKey: null,
  rescanning: false,
  onApply: noop,
  onReview: noop,
  onDismiss: noop,
  onRestoreAll: noop,
  onRescan: noop,
};

describe("MesaSuggestions", () => {
  it("renders the empty state with a reachable Rescan when nothing was found", () => {
    // The card must never disappear entirely: an early or empty scan would
    // otherwise hide the Rescan affordance and the feature itself.
    const { getByText } = render(<MesaSuggestions {...baseProps} suggestions={[]} />);
    expect(getByText("No open suggestions.")).toBeTruthy();
    expect(getByText("Rescan")).toBeTruthy();
  });

  it("renders rows with subject, badges, and reason", () => {
    const { container, getByText } = render(
      <MesaSuggestions {...baseProps} suggestions={[lockRow, domainRow]} />,
    );
    expect(getByText("lock.front")).toBeTruthy();
    // The mode pill shows the translated label now, not the raw slug it used to
    // render; the slug still drives the badge colour and the apply payload.
    expect(getByText("Prohibited")).toBeTruthy();
    expect(getByText(/physical security boundary/)).toBeTruthy();
    // Domain-scope rows are labelled as such.
    expect(getByText("update.* (domain)")).toBeTruthy();
    // Count badge reflects the open suggestions.
    expect(container.querySelector(".mesa-suggest-count")?.textContent).toBe("2");
  });

  it("fires the right callback with the row", () => {
    const onApply = vi.fn();
    const onReview = vi.fn();
    const onDismiss = vi.fn();
    const { getAllByText } = render(
      <MesaSuggestions
        {...baseProps}
        suggestions={[lockRow]}
        onApply={onApply}
        onReview={onReview}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(getAllByText("Apply")[0]);
    fireEvent.click(getAllByText("Review")[0]);
    fireEvent.click(getAllByText("Dismiss")[0]);
    expect(onApply).toHaveBeenCalledWith(lockRow);
    expect(onReview).toHaveBeenCalledWith(lockRow);
    expect(onDismiss).toHaveBeenCalledWith(lockRow);
  });

  it("shows the restore-all footer when only dismissals exist", () => {
    const onRestoreAll = vi.fn();
    const { getByText } = render(
      <MesaSuggestions {...baseProps} suggestions={[]} dismissedCount={3} onRestoreAll={onRestoreAll} />,
    );
    expect(getByText("3 dismissed")).toBeTruthy();
    fireEvent.click(getByText("Restore all"));
    expect(onRestoreAll).toHaveBeenCalled();
    expect(getByText("No open suggestions.")).toBeTruthy();
  });

  it("disables only the busy row's own actions, not Rescan or the toggle", () => {
    const { container, getByText } = render(
      <MesaSuggestions {...baseProps} suggestions={[lockRow]} busyKey={lockRow.key} />,
    );
    const row = container.querySelector(".mesa-suggest-row") as HTMLElement;
    for (const b of Array.from(row.querySelectorAll("button"))) {
      expect((b as HTMLButtonElement).disabled).toBe(true);
    }
    expect((getByText("Rescan") as HTMLButtonElement).disabled).toBe(false);
    expect(container.querySelector(".mesa-suggest-toggle")).not.toBeNull();
  });

  it("toggles collapsed/expanded via the disclosure chevron, defaulting open", () => {
    const { container, getByText, queryByText } = render(
      <MesaSuggestions {...baseProps} suggestions={[lockRow]} />,
    );
    const toggle = container.querySelector(".mesa-suggest-toggle") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".collapsible-chevron.open")).not.toBeNull();
    expect(getByText("lock.front")).toBeTruthy();
    expect(getByText("Rescan")).toBeTruthy();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(queryByText("lock.front")).toBeNull();
    // Rescan hides along with the rest of the body while collapsed, matching
    // the other collapsible cards on this tab.
    expect(queryByText("Rescan")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(getByText("lock.front")).toBeTruthy();
    expect(getByText("Rescan")).toBeTruthy();
  });
});
