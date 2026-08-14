import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import type { ApprovalRecord } from "../types";

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), getApproval: vi.fn() },
}));

function record(over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id: "appr_1",
    token_id: "t1",
    token_name: "my_token",
    tool_name: "create_automation",
    cap_name: "cap_automation_write",
    args: {},
    diff: { summary: "Create automation 'Front door notify'" },
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

describe("ApprovalsView pending pagination", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("phx-approval-default-view", "details");
  });

  it("loads the first pending page with limit/offset and shows Load more when more remain", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record({ id: "appr_1", diff: { summary: "First pending" } })],
      total: 2, limit: 50, offset: 0,
    });

    const { findByText, queryByText } = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
    await findByText("First pending");

    expect(api.listApprovals).toHaveBeenCalledWith({ status: "pending", limit: 50, offset: 0 });
    expect(queryByText("Load more")).toBeTruthy();
  });

  it("Load more appends the next page at the accumulated offset", async () => {
    vi.mocked(api.listApprovals)
      .mockResolvedValueOnce({
        approvals: [record({ id: "appr_1", diff: { summary: "First pending" } })],
        total: 2, limit: 50, offset: 0,
      })
      .mockResolvedValueOnce({
        approvals: [record({ id: "appr_2", diff: { summary: "Second pending" } })],
        total: 2, limit: 50, offset: 1,
      });

    const { findByText, getByText, queryByText } = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
    await findByText("First pending");

    fireEvent.click(getByText("Load more"));
    await findByText("Second pending");

    expect(api.listApprovals).toHaveBeenLastCalledWith({ status: "pending", limit: 50, offset: 1 });
    expect(queryByText("Load more")).toBeNull();
  });

  it("closes an open detail modal when its approval is resolved elsewhere", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record({ id: "appr_1", diff: { summary: "Front door notify" } })],
      total: 1, limit: 50, offset: 0,
    });
    const onCountChange = vi.fn();
    const { rerender, findByText, container } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} onCountChange={onCountChange} refreshSignal={0} />,
    );

    // Open the detail modal (the "Review..." deep-link lands here too).
    fireEvent.click(await findByText("Front door notify"));
    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeTruthy());

    // The approval is approved elsewhere (inline Approve in Agent Chat): the list
    // now returns nothing and getApproval reports it resolved. The event bumps
    // refreshSignal, which must close the now-stale modal.
    vi.mocked(api.getApproval).mockResolvedValue(
      record({ id: "appr_1", status: "approved", resolved_at: "2026-01-01T00:30:00Z" }),
    );
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [], total: 0, limit: 50, offset: 0 });
    rerender(<ApprovalsView tab="pending" onTabChange={() => {}} onCountChange={onCountChange} refreshSignal={1} />);

    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeNull());
    expect(api.getApproval).toHaveBeenCalledWith("appr_1");
    expect(onCountChange).toHaveBeenCalled();
  });

  it("closes an open detail modal when its approval is rejected elsewhere", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record({ id: "appr_1", diff: { summary: "Front door notify" } })],
      total: 1, limit: 50, offset: 0,
    });
    const { rerender, findByText, container } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} refreshSignal={0} />,
    );
    fireEvent.click(await findByText("Front door notify"));
    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeTruthy());

    // Rejected elsewhere (e.g. inline Reject in Agent Chat) fires the same
    // resolved event; the non-pending status must close the stale modal too.
    vi.mocked(api.getApproval).mockResolvedValue(
      record({ id: "appr_1", status: "rejected", resolved_at: "2026-01-01T00:30:00Z",
               rejected_reason: "not now" }),
    );
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [], total: 0, limit: 50, offset: 0 });
    rerender(<ApprovalsView tab="pending" onTabChange={() => {}} refreshSignal={1} />);

    await waitFor(() => expect(container.querySelector("#approval-detail-tab-diff")).toBeNull());
    expect(api.getApproval).toHaveBeenCalledWith("appr_1");
  });
});
