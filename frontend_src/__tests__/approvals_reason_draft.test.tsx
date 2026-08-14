import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import { getReasonDraft, setReasonDraft } from "../utils/approval_reason_draft";
import type { ApprovalRecord } from "../types";

// The rejection reason typed in this modal is shared with the Agent Chat
// window's approval bubble, whose Reject button has no reason field of its own.
// The two live in different bundles when the global chat window is enabled, so
// the hand-off goes through localStorage rather than module state.

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), getApproval: vi.fn(), rejectApproval: vi.fn(), approveApproval: vi.fn() },
  currentHass: () => ({}),
}));

function record(over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id: "appr_1",
    token_id: "t1",
    token_name: "my_token",
    tool_name: "create_automation",
    cap_name: "cap_automation_write",
    args: {},
    diff: { kind: "config_diff", summary: "Create automation 'x'", after: "{}" },
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

async function openDetail() {
  const view = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
  fireEvent.click(await view.findByText("Create automation 'x'"));
  await waitFor(() => expect(view.container.querySelector("#approval-detail-tab-diff")).toBeTruthy());
  return view;
}

describe("rejection reason draft sharing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("phx-approval-default-view", "details");
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record()], total: 1, limit: 50, offset: 0,
    });
  });

  it("stashes what is typed so another surface can send it", async () => {
    const { findByLabelText } = await openDetail();
    const input = await findByLabelText("Optional rejection reason");
    fireEvent.change(input, { target: { value: "use the outdoor sensor" } });
    expect(getReasonDraft("appr_1")).toBe("use the outdoor sensor");

    // Clearing the box clears the draft: no stale reason gets attached later.
    fireEvent.change(input, { target: { value: "" } });
    expect(getReasonDraft("appr_1")).toBe("");
  });

  it("restores the draft when the modal is reopened", async () => {
    setReasonDraft("appr_1", "put it on the Kitchen view");
    const { findByLabelText } = await openDetail();
    const input = await findByLabelText("Optional rejection reason") as HTMLInputElement;
    expect(input.value).toBe("put it on the Kitchen view");
  });

  it("clears the draft once the approval is rejected here", async () => {
    setReasonDraft("appr_1", "wrong entity");
    vi.mocked(api.rejectApproval).mockResolvedValue(
      record({ status: "rejected", rejected_reason: "wrong entity" }));

    const { findByRole } = await openDetail();
    fireEvent.click(await findByRole("button", { name: "Reject" }));

    await waitFor(() => expect(api.rejectApproval).toHaveBeenCalledWith(
      "appr_1", { reason: "wrong entity" }));
    await waitFor(() => expect(getReasonDraft("appr_1")).toBe(""));
  });
});
