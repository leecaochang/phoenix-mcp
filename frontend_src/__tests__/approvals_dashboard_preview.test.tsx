import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import type { ApprovalRecord } from "../types";

// Regression coverage for a live bug: the Diff|Preview toggle was wired only
// for set_dashboard_config approvals, so an add/edit/delete_dashboard_card
// approval (the card-level tools) never offered a preview at all, even with
// <hui-card> registered. Every dashboard-write tool must be covered.
//
// Like yamlview.test.tsx (the ha-code-editor precedent), the "not registered"
// case is asserted FIRST, before this file ever calls customElements.define:
// jsdom's custom element registry is shared for the whole file and cannot be
// un-registered, so test order carries the coverage.

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), getApproval: vi.fn() },
  currentHass: () => ({}),
}));

function record(over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id: "appr_1",
    token_id: "t1",
    token_name: "my_token",
    tool_name: "set_dashboard_config",
    cap_name: "cap_lovelace_write",
    args: {},
    diff: { kind: "yaml_diff", summary: "Set dashboard layout" },
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-01T01:00:00Z",
    resolved_at: null,
    approved_by_user_id: null,
    rejected_reason: null,
    result: null,
    request_id: "r1",
    client_ip: null,
    ...over,
  };
}

async function openDetail(rec: ApprovalRecord) {
  vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [rec], total: 1, limit: 50, offset: 0 });
  const utils = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
  fireEvent.click(await utils.findByText(rec.diff.summary!));
  await waitFor(() => expect(utils.container.querySelector("#approval-detail-tab-diff")).toBeTruthy());
  return utils;
}

describe("dashboard preview toggle on the approval card", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The Diff|Preview choice is persisted; clear it so each test starts from
    // the default (diff) regardless of what an earlier test clicked.
    localStorage.clear();
  });

  it("offers no Preview button before hui-card is registered, even for a previewable layout", async () => {
    const { queryByRole } = await openDetail(record({
      tool_name: "set_dashboard_config",
      args: { config: { views: [{ title: "Home", cards: [{ type: "markdown", content: "hi" }] }] } },
    }));
    expect(queryByRole("button", { name: "Preview" })).toBeNull();
  });

  it("registers hui-card (as a session that has opened any dashboard would)", () => {
    // Mirrors yamlview.test.tsx's ha-code-editor registration: from here on in
    // this file, hui-card is available and whenDefined resolves immediately.
    customElements.define("hui-card", class extends HTMLElement {});
    expect(customElements.get("hui-card")).toBeTruthy();
  });

  it("offers Preview for a set_dashboard_config approval, fed from args.config", async () => {
    const { container, findByRole } = await openDetail(record({
      tool_name: "set_dashboard_config",
      args: { config: { views: [{ title: "Home", cards: [{ type: "markdown", content: "hi" }] }] } },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
  });

  it("offers Preview for an add_dashboard_card approval (the reported gap)", async () => {
    const { container, findByRole, queryByRole } = await openDetail(record({
      tool_name: "add_dashboard_card",
      diff: {
        kind: "yaml_diff", summary: "Add 'history-graph' to dashboard",
        after: JSON.stringify({ type: "history-graph", entities: ["sensor.solar"] }),
      },
      args: { view_index: 0, card: { type: "history-graph", entities: ["sensor.solar"] } },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
    // add has no Before card: no side toggle.
    expect(queryByRole("button", { name: "Before" })).toBeNull();
  });

  it("offers a Before|After side toggle for edit_dashboard_card, defaulting to After", async () => {
    const { container, findByRole, getByRole } = await openDetail(record({
      tool_name: "edit_dashboard_card",
      diff: {
        kind: "yaml_diff", summary: "Replace card 1 on dashboard",
        before: JSON.stringify({ type: "markdown", content: "old" }),
        after: JSON.stringify({ type: "markdown", content: "new" }),
      },
      args: { view_index: 0, card_index: 1, card: { type: "markdown", content: "new" } },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(getByRole("button", { name: "Before" })).toBeTruthy();
    expect(getByRole("button", { name: "After" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(getByRole("button", { name: "Before" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
  });

  it("defaults to the Before side for delete_dashboard_card and offers no side toggle (no After exists)", async () => {
    const { container, findByRole, queryByRole } = await openDetail(record({
      tool_name: "delete_dashboard_card",
      diff: {
        kind: "yaml_diff", summary: "Delete card 0 from dashboard",
        before: JSON.stringify({ type: "markdown", content: "gone" }),
      },
      args: { view_index: 0, card_index: 0 },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
    expect(queryByRole("button", { name: "Before" })).toBeNull();
  });

  it("offers no Preview button when the layout has no previewable shape (e.g. a strategy dashboard)", async () => {
    const { queryByRole } = await openDetail(record({
      tool_name: "set_dashboard_config",
      args: { config: { strategy: { type: "original-states" } } },
    }));
    expect(queryByRole("button", { name: "Preview" })).toBeNull();
  });

  it("offers Preview for a big card whose diff string was truncated, via args.card (the reported gap)", async () => {
    // A multi-series chart card can exceed the diff's truncation bound, so
    // diff.after is cut mid-JSON and unparseable; the full card object still
    // rides on the approval's args and must feed the preview instead.
    const bigCard = {
      type: "custom:apexcharts-card",
      series: Array.from({ length: 30 }, (_, i) => ({ entity: `sensor.t_${i}`, name: `Room ${i}` })),
    };
    const truncated = `${JSON.stringify(bigCard, null, 2).slice(0, 4000)}\n... (1735 more characters)`;
    const { container, findByRole } = await openDetail(record({
      tool_name: "add_dashboard_card",
      diff: { kind: "yaml_diff", summary: "Add 'custom:apexcharts-card' to dashboard", after: truncated },
      args: { view_index: 0, card: bigCard },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
  });

  it("remembers the Preview choice: the next approval opens in Preview without a click", async () => {
    const rec = record({
      tool_name: "set_dashboard_config",
      args: { config: { views: [{ title: "Home", cards: [{ type: "markdown", content: "hi" }] }] } },
    });
    const first = await openDetail(rec);
    fireEvent.click(await first.findByRole("button", { name: "Preview" }));
    expect(first.container.querySelector("hui-card")).toBeTruthy();
    first.unmount();

    const second = await openDetail(record({
      ...rec,
      id: "appr_2",
      diff: { kind: "yaml_diff", summary: "Another layout" },
    }));
    // No click: the persisted preference opens the diff area in Preview mode.
    await waitFor(() => expect(second.container.querySelector("hui-card")).toBeTruthy());
  });

  it("offers Preview for a patch_dashboard approval that targets a card", async () => {
    const { container, findByRole } = await openDetail(record({
      tool_name: "patch_dashboard",
      diff: {
        kind: "yaml_diff", summary: "Set views[0].cards[1] on dashboard",
        after: JSON.stringify({ type: "markdown", content: "patched" }),
      },
      args: { path: ["views", 0, "cards", 1], value: { type: "markdown", content: "patched" } },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
  });

  it("offers NO Preview for a patch_dashboard approval that targets a badge", async () => {
    // A badge is its own config shape. Feeding it to hui-card would render
    // something that is not what the approver is approving, which is worse than
    // showing no picture, so these fall back to the text diff on purpose.
    const { queryByRole } = await openDetail(record({
      tool_name: "patch_dashboard",
      diff: {
        kind: "yaml_diff", summary: "Set views[0].badges[4].entity on dashboard",
        after: JSON.stringify("sensor.laundry_remaining"),
      },
      args: { path: ["views", 0, "badges", 4, "entity"], value: "sensor.laundry_remaining" },
    }));
    expect(queryByRole("button", { name: "Preview" })).toBeNull();
  });

  it("defaults a remove patch to the Before side, since it has no After", async () => {
    const { container, findByRole } = await openDetail(record({
      tool_name: "patch_dashboard",
      diff: {
        kind: "yaml_diff", summary: "Remove views[0].cards[1] from dashboard",
        before: JSON.stringify({ type: "markdown", content: "going away" }),
      },
      args: { path: ["views", 0, "cards", 1], op: "remove" },
    }));
    fireEvent.click(await findByRole("button", { name: "Preview" }));
    expect(container.querySelector("hui-card")).toBeTruthy();
  });

  it("renders the Diff|Preview switch inside the diff toolbar next to the layout toggle", async () => {
    const { container, getByRole } = await openDetail(record({
      tool_name: "set_dashboard_config",
      args: { config: { views: [{ title: "Home", cards: [{ type: "markdown", content: "hi" }] }] } },
    }));
    // Diff mode: one toolbar row holds the hint, the mode switch, and the
    // side-by-side/stacked layout toggle (the switch must not sit in its own row).
    const toolbar = container.querySelector(".change-diff-toolbar")!;
    expect(toolbar.querySelector('[aria-label="Diff display mode"]')).toBeTruthy();
    expect(toolbar.querySelector(".diff-layout-toggle")).toBeTruthy();
    // Preview mode keeps the switch in the toolbar (no layout toggle there).
    fireEvent.click(getByRole("button", { name: "Preview" }));
    const previewToolbar = container.querySelector(".change-diff-toolbar")!;
    expect(previewToolbar.querySelector('[aria-label="Diff display mode"]')).toBeTruthy();
    expect(previewToolbar.querySelector(".diff-layout-toggle")).toBeNull();
  });
});
