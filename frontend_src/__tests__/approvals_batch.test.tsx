import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import type { ApprovalRecord } from "../types";

// Batch approve is an operator convenience only: the server still runs each id
// through the same single-approval path. These cover the panel's own contract,
// which is selection (what may be ticked), ordering (what is sent), and the
// three-part result, since "approved 6" alone reads as success on a run that
// stopped at item 7.

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), getApproval: vi.fn(), batchApproveApprovals: vi.fn() },
  currentHass: () => ({}),
}));

function record(id: string, over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id,
    token_id: "t1",
    token_name: "my_token",
    tool_name: "edit_automation",
    cap_name: "cap_automation_write",
    args: {},
    diff: { kind: "yaml_diff", summary: `Edit ${id}` },
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T01:00:00Z",
    resolved_at: null,
    approved_by_user_id: null,
    rejected_reason: null,
    result: null,
    request_id: "r1",
    client_ip: null,
    ...over,
  };
}

async function renderPending(records: ApprovalRecord[], props = {}) {
  vi.mocked(api.listApprovals).mockResolvedValue({
    approvals: records, total: records.length, limit: 50, offset: 0,
  });
  const utils = render(
    <ApprovalsView tab="pending" onTabChange={() => {}} {...props} />,
  );
  if (records.length) await utils.findByText(records[0].diff.summary!);
  return utils;
}

describe("batch approve", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("shows no batch bar for a single pending approval", async () => {
    const { queryByText } = await renderPending([record("a")]);
    expect(queryByText("Select all")).toBeNull();
  });

  it("sends the ticked ids in the order shown", async () => {
    vi.mocked(api.batchApproveApprovals).mockResolvedValue({
      applied: [{ approval_id: "a", tool_name: "edit_automation" },
                { approval_id: "c", tool_name: "edit_automation" }],
      failed: null,
      remaining: [],
    });
    const { getByRole } = await renderPending(
      [record("a"), record("b"), record("c")],
    );
    // Tick c first, then a: the request must still follow list order, so that a
    // "stopped at" names a row the operator can find. Addressed positionally
    // because all three rows carry the same tool name and so the same label.
    const boxes = document.querySelectorAll<HTMLInputElement>(".approval-card-check");
    fireEvent.click(boxes[2]);
    fireEvent.click(boxes[0]);
    fireEvent.click(getByRole("button", { name: "Approve 2 selected" }));
    await waitFor(() => expect(api.batchApproveApprovals).toHaveBeenCalled());
    expect(vi.mocked(api.batchApproveApprovals).mock.calls[0][0]).toEqual(["a", "c"]);
  });

  it("select-all ticks every selectable row", async () => {
    const { getByRole, findByRole } = await renderPending([record("a"), record("b")]);
    fireEvent.click(getByRole("checkbox", { name: "Select all" }));
    expect(await findByRole("button", { name: "Approve 2 selected" })).toBeTruthy();
  });

  it("will not tick an approval that is already executing", async () => {
    // The server would answer 409 on its claim and halt the run on a row the
    // operator never chose, so it is not selectable in the first place.
    await renderPending([record("a", { in_progress: true }), record("b")]);
    const boxes = document.querySelectorAll<HTMLInputElement>(".approval-card-check");
    expect(boxes[0].disabled).toBe(true);
    expect(boxes[1].disabled).toBe(false);
  });

  it("reports applied, the stopping point, and what is left pending", async () => {
    vi.mocked(api.batchApproveApprovals).mockResolvedValue({
      applied: [{ approval_id: "a", tool_name: "edit_automation" }],
      failed: {
        approval_id: "b", tool_name: "edit_automation", status: 409,
        error: "conflict", message: "Capability is now denied for this token.",
      },
      remaining: ["c"],
    });
    const { getByRole, findByText } = await renderPending(
      [record("a"), record("b"), record("c")],
    );
    fireEvent.click(getByRole("checkbox", { name: "Select all" }));
    fireEvent.click(getByRole("button", { name: "Approve 3 selected" }));

    expect(await findByText("Approved 1.")).toBeTruthy();
    expect(await findByText(/Capability is now denied/)).toBeTruthy();
    // The remaining count is the fact the operator acts on: untouched, not lost.
    expect(await findByText("1 left pending.")).toBeTruthy();
  });

  it("keeps only the untouched remainder ticked after a stop", async () => {
    vi.mocked(api.batchApproveApprovals).mockResolvedValue({
      applied: [{ approval_id: "a", tool_name: "edit_automation" }],
      failed: { approval_id: "b", tool_name: "edit_automation", status: 500, error: "internal_error" },
      remaining: ["c"],
    });
    const { getByRole, findByRole } = await renderPending(
      [record("a"), record("b"), record("c")],
    );
    fireEvent.click(getByRole("checkbox", { name: "Select all" }));
    fireEvent.click(getByRole("button", { name: "Approve 3 selected" }));
    // One click retries exactly the remainder once the cause is dealt with.
    expect(await findByRole("button", { name: "Approve 1 selected" })).toBeTruthy();
  });
});
