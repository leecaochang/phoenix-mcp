import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import type { ApprovalRecord } from "../types";

// One-click "Reject with error message": when the live preview detects a card
// configuration error (HA swaps in hui-error-card), the approval modal offers
// a third action that rejects with that error as the reason, so the operator
// never has to copy-paste it back to the agent by hand.
//
// This file registers its OWN hui-card stub that always spawns an error card
// (jsdom's custom element registry is shared per test file, which is why this
// lives apart from approvals_dashboard_preview.test.tsx).

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), getApproval: vi.fn(), rejectApproval: vi.fn() },
  currentHass: () => ({}),
}));

customElements.define("hui-card", class extends HTMLElement {
  connectedCallback() {
    const err = document.createElement("hui-error-card");
    (err as unknown as { _config: unknown })._config = {
      error: "value.series[0] is not a ChartCardSeriesExternalConfig",
    };
    this.appendChild(err);
  }
});

function record(over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id: "appr_1",
    token_id: "t1",
    token_name: "my_token",
    tool_name: "add_dashboard_card",
    cap_name: "cap_lovelace_write",
    args: { view_index: 0, card: { type: "custom:apexcharts-card", series: [] } },
    diff: { kind: "yaml_diff", summary: "Add 'custom:apexcharts-card' to dashboard" },
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

describe("Reject with error message", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Open straight into Preview mode so error detection runs immediately.
    localStorage.setItem("phx-dash-preview-mode", "preview");
  });

  it("appears once the preview detects a config error and rejects with it as the reason", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record()], total: 1, limit: 50, offset: 0,
    });
    vi.mocked(api.rejectApproval).mockResolvedValue(record({ status: "rejected", rejected_reason: "x" }));

    const { container, findByText, findByRole } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} />,
    );
    fireEvent.click(await findByText("Add 'custom:apexcharts-card' to dashboard"));
    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeTruthy());

    // The stubbed hui-card spawns an error card; detection reports it and the
    // third action appears alongside Approve/Reject.
    const btn = await findByRole("button", { name: "Reject with error message" });
    fireEvent.click(btn);

    await waitFor(() => expect(api.rejectApproval).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.rejectApproval).mock.calls[0];
    expect((body as { reason: string }).reason).toContain("Card configuration error:");
    expect((body as { reason: string }).reason).toContain("value.series[0] is not a ChartCardSeriesExternalConfig");
  });

  it("offers no such button when the preview reports no errors", async () => {
    // A non-dashboard approval renders no preview at all: the button must not exist.
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record({
        tool_name: "create_automation",
        cap_name: "cap_automation_write",
        args: {},
        diff: { kind: "config_diff", summary: "Create automation 'x'", after: "{}" },
      })],
      total: 1, limit: 50, offset: 0,
    });
    const { container, findByText, queryByRole } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} />,
    );
    fireEvent.click(await findByText("Create automation 'x'"));
    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeTruthy());
    expect(queryByRole("button", { name: "Reject with error message" })).toBeNull();
  });
});
