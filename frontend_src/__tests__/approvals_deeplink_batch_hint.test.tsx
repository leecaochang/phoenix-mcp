import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import { AGENTCHAT_REVIEW_CLOSED_EVENT } from "../utils/agentchat_review";
import type { ApprovalRecord } from "../types";

// A persistent notification links to ONE approval, so an operator who works from
// notifications lands in the detail modal and never meets the pending list, where
// the tick boxes and the batch bar live. Batch approve was therefore built on a
// surface that operator rarely opens. These cover the hint that closes that gap:
// what it says, and the three ways it must stay quiet.
//
// It routes to the list rather than offering approve-all from the modal, and that
// is the point rather than an omission: everything else in the queue is unreviewed,
// and approval is the operator's intent to look at each change.

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

/** Render as if arriving from a notification deep-link onto `openId`. */
async function renderDeepLink(
  records: ApprovalRecord[],
  openId: string,
  props: Record<string, unknown> = {},
  total = records.length,
) {
  vi.mocked(api.listApprovals).mockResolvedValue({
    approvals: records, total, limit: 50, offset: 0,
  });
  const opened = records.find((r) => r.id === openId);
  vi.mocked(api.getApproval).mockResolvedValue(opened as ApprovalRecord);
  const utils = render(
    <ApprovalsView tab="pending" onTabChange={() => {}} openApprovalId={openId} {...props} />,
  );
  await waitFor(() => expect(api.getApproval).toHaveBeenCalled());
  return utils;
}

describe("deep-link batch hint", () => {
  beforeEach(() => vi.clearAllMocks());

  it("tells the operator how many others are waiting", async () => {
    const { findByText } = await renderDeepLink(
      [record("a"), record("b"), record("c")], "a",
    );
    expect(await findByText("2 other approvals are waiting for review.")).toBeTruthy();
  });

  it("counts the whole queue, not just the loaded page", async () => {
    // The pending list pages, so counting rendered rows would under-report a
    // long queue to the operator deciding whether it is worth opening.
    const { findByText } = await renderDeepLink(
      [record("a"), record("b")], "a", {}, 12,
    );
    expect(await findByText("11 other approvals are waiting for review.")).toBeTruthy();
  });

  it("uses the singular form for exactly one other", async () => {
    const { findByText } = await renderDeepLink([record("a"), record("b")], "a");
    expect(await findByText("1 other approval is waiting for review.")).toBeTruthy();
  });

  it("says nothing when this is the only approval", async () => {
    const { findByRole, queryByText } = await renderDeepLink([record("a")], "a");
    await findByRole("dialog");
    expect(queryByText(/waiting for review/)).toBeNull();
  });

  it("Review all closes the modal and leaves the queue on screen", async () => {
    const closed = vi.fn();
    window.addEventListener(AGENTCHAT_REVIEW_CLOSED_EVENT, closed);
    const { findByText, queryByRole, getAllByText } = await renderDeepLink(
      [record("a"), record("b"), record("c")], "a",
    );
    try {
      fireEvent.click(await findByText("Review all"));
      await waitFor(() => expect(queryByRole("dialog")).toBeNull());
      expect(closed).toHaveBeenCalledOnce();
      expect((closed.mock.calls[0][0] as CustomEvent).detail)
        .toEqual({ approvalId: "a" });
      // The batch bar only renders from two selectable rows up, so its presence
      // is the actual proof the operator has now met batching.
      expect(getAllByText("Select all").length).toBeGreaterThan(0);
    } finally {
      window.removeEventListener(AGENTCHAT_REVIEW_CLOSED_EVENT, closed);
    }
  });

  it("stays quiet when the modal was opened from the list", async () => {
    // Not a redundancy nicety: from the list the batch bar is already on screen
    // behind the modal, and a banner that is always there is one that is skipped.
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record("a"), record("b"), record("c")], total: 3, limit: 50, offset: 0,
    });
    const { findAllByText, findByRole, queryByText } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} />,
    );
    fireEvent.click((await findAllByText("Edit a"))[0]);
    await findByRole("dialog");
    expect(queryByText(/waiting for review/)).toBeNull();
  });

  it("stays quiet for a resolved record even before the tab catches up", async () => {
    // The parent moves the sub-tab to History for a resolved deep-link, but that
    // is a round trip: for the render in between, the tab is still Pending while
    // the open record is not, and without the status check the banner flashes a
    // count onto an approval that is already decided.
    const resolved = record("a", { status: "approved", resolved_at: "2026-01-01T00:05:00Z" });
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record("b"), record("c")], total: 2, limit: 50, offset: 0,
    });
    vi.mocked(api.getApproval).mockResolvedValue(resolved);
    const { findByRole, queryByText } = render(
      <ApprovalsView tab="pending" onTabChange={() => {}} openApprovalId="a" />,
    );
    await findByRole("dialog");
    expect(queryByText(/waiting for review/)).toBeNull();
  });

  it("forgets the deep-link once the modal is closed", async () => {
    // Reopening the SAME approval from the list is an operator who is looking at
    // the queue, so the banner must not linger from how they first arrived.
    const { findByText, findAllByText, findByRole, queryByText } = await renderDeepLink(
      [record("a"), record("b"), record("c")], "a",
    );
    await findByText("2 other approvals are waiting for review.");
    fireEvent.click(await findByText("Review all"));
    await waitFor(() => expect(queryByText(/waiting for review/)).toBeNull());
    fireEvent.click((await findAllByText("Edit a"))[0]);
    await findByRole("dialog");
    expect(queryByText(/waiting for review/)).toBeNull();
  });

  it("stays quiet for an already-resolved deep-linked approval", async () => {
    // That link lands on History, where pendingTotal is not refreshed, so a count
    // shown there would be whatever the last pending fetch happened to leave.
    const resolved = record("a", { status: "approved", resolved_at: "2026-01-01T00:05:00Z" });
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record("b"), record("c")], total: 2, limit: 50, offset: 0,
    });
    vi.mocked(api.getApproval).mockResolvedValue(resolved);
    const { findByRole, queryByText } = render(
      <ApprovalsView tab="history" onTabChange={() => {}} openApprovalId="a" />,
    );
    await findByRole("dialog");
    expect(queryByText(/waiting for review/)).toBeNull();
  });

  it("leaves an unconsumed deep-link intact when the panel unmounts mid-fetch", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({
      approvals: [record("a")], total: 1, limit: 50, offset: 0,
    });
    let resolveApproval!: (value: ApprovalRecord) => void;
    vi.mocked(api.getApproval).mockReturnValue(new Promise((resolve) => {
      resolveApproval = resolve;
    }));
    const onConsumedDeepLink = vi.fn();
    const { unmount } = render(
      <ApprovalsView
        tab="pending"
        onTabChange={() => {}}
        openApprovalId="a"
        onConsumedDeepLink={onConsumedDeepLink}
      />,
    );
    await waitFor(() => expect(api.getApproval).toHaveBeenCalledWith("a"));

    unmount();
    await act(async () => {
      resolveApproval(record("a"));
      await Promise.resolve();
    });

    expect(onConsumedDeepLink).not.toHaveBeenCalled();
  });
});
