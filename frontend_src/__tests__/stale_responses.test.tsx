/** A response a newer request superseded must never reach the screen.
 *
 *  Both list views load from six or so places with no ordering between them
 *  (mount, poll tick, HA event, manual refresh, tab or filter change, Load
 *  more), all writing the same state. Every existing test resolves its mocks in
 *  call order, which is the one ordering that cannot show the bug: the failure
 *  is a slow OLD request landing after a fast NEW one and putting the previous
 *  filter's rows back, where they sit until something happens to reload.
 *
 *  So these resolve the promises out of order, deliberately.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const listApprovals = vi.fn();
const getApproval = vi.fn();
const listVersions = vi.fn();
const listTokens = vi.fn();
const listArchivedTokens = vi.fn();

vi.mock("../api", () => ({
  api: {
    listApprovals: (...a: unknown[]) => listApprovals(...a),
    getApproval: (...a: unknown[]) => getApproval(...a),
    approveApproval: vi.fn(),
    rejectApproval: vi.fn(),
    batchApproveApprovals: vi.fn(),
    listVersions: (...a: unknown[]) => listVersions(...a),
    listTokens: (...a: unknown[]) => listTokens(...a),
    listArchivedTokens: (...a: unknown[]) => listArchivedTokens(...a),
  },
}));

import { ApprovalsView } from "../views/ApprovalsView";
import { ChangesView } from "../views/ChangesView";

/** A promise whose resolution this test controls. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

function approval(id: string, status: string) {
  return {
    id,
    token_id: "t1",
    token_name: "tok",
    tool_name: "call_service",
    cap_name: "cap_physical_control",
    args: {},
    diff: { kind: "yaml_diff", summary: `Edit ${id}` },
    status,
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T01:00:00Z",
    resolved_at: null,
    approved_by_user_id: null,
    rejected_reason: null,
    result: null,
    request_id: "r",
    client_ip: null,
  };
}

function version(alias: string) {
  return {
    id: alias,
    resource_type: "automation",
    resource_id: "aid1",
    alias,
    action: "create",
    token_id: "t1",
    token_name: "my_token",
    approved_by_user_id: null,
    timestamp: "2026-01-01T00:00:00Z",
    has_before: false,
    has_after: true,
  };
}

describe("ApprovalsView discards a superseded response", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getApproval.mockResolvedValue(approval("a1", "pending"));
  });

  it("a slow refresh cannot overwrite the newer one that overtook it", async () => {
    const slowOld = deferred<{ approvals: unknown[]; total: number; limit: number; offset: number }>();
    const fastNew = deferred<{ approvals: unknown[]; total: number; limit: number; offset: number }>();
    // Call 1 is the mount. Calls 2 and 3 are two HA approval events arriving
    // close together, which is the ordinary way two loads overlap here: the
    // parent bumps refreshSignal on each one and neither waits for the other.
    listApprovals
      .mockResolvedValueOnce({ approvals: [], total: 0, limit: 50, offset: 0 })
      .mockReturnValueOnce(slowOld.promise)
      .mockReturnValueOnce(fastNew.promise);

    const { rerender } = render(
      <ApprovalsView tab="history" onTabChange={() => {}} refreshSignal={0} />);
    await waitFor(() => expect(listApprovals).toHaveBeenCalledTimes(1));

    rerender(<ApprovalsView tab="history" onTabChange={() => {}} refreshSignal={1} />);
    await waitFor(() => expect(listApprovals).toHaveBeenCalledTimes(2));
    rerender(<ApprovalsView tab="history" onTabChange={() => {}} refreshSignal={2} />);
    await waitFor(() => expect(listApprovals).toHaveBeenCalledTimes(3));

    // The NEWER request answers first, then the older one lands.
    fastNew.resolve({ approvals: [approval("new-row", "approved")], total: 1, limit: 50, offset: 0 });
    await screen.findByText(/Edit new-row/);
    slowOld.resolve({ approvals: [approval("stale-row", "rejected")], total: 1, limit: 50, offset: 0 });

    await waitFor(() => expect(screen.queryByText(/Edit new-row/)).not.toBeNull());
    expect(screen.queryByText(/Edit stale-row/)).toBeNull();
  });
});

describe("ChangesView discards a superseded response", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTokens.mockResolvedValue([]);
    listArchivedTokens.mockResolvedValue([]);
  });

  it("a slow first page cannot land after a newer refresh", async () => {
    type Page = { resource_type: null; resource_id: null; versions: unknown[]; total: number };
    const slowFirst = deferred<Page>();
    const fastSecond = deferred<Page>();
    listVersions
      .mockReturnValueOnce(slowFirst.promise)
      .mockReturnValueOnce(fastSecond.promise);

    render(<ChangesView hass={null} />);
    await waitFor(() => expect(listVersions).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText("Refresh changes"));
    await waitFor(() => expect(listVersions).toHaveBeenCalledTimes(2));

    fastSecond.resolve({ resource_type: null, resource_id: null, versions: [version("fresh")], total: 1 });
    await screen.findByText(/fresh/);
    slowFirst.resolve({ resource_type: null, resource_id: null, versions: [version("stale")], total: 1 });

    await waitFor(() => expect(screen.queryByText(/fresh/)).not.toBeNull());
    expect(screen.queryByText(/stale/)).toBeNull();
  });
});
