import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor, within } from "@testing-library/react";
import { ApprovalsView } from "../views/ApprovalsView";
import { api } from "../api";
import type { ApprovalRecord } from "../types";

vi.mock("../api", () => ({
  api: { listApprovals: vi.fn(), approveApproval: vi.fn(), rejectApproval: vi.fn(), getApproval: vi.fn() },
  currentHass: () => ({}),
}));

function record(over: Partial<ApprovalRecord> = {}): ApprovalRecord {
  return {
    id: "appr_summary",
    token_id: "t1",
    token_name: "foxy_token",
    tool_name: "restart_ha",
    cap_name: "cap_restart",
    args: { technical: true },
    diff: { kind: "system_action", summary: "Restart Home Assistant", summary_key: "diff.restart_ha", summary_params: {} },
    status: "pending",
    created_at: "2026-08-13T04:00:00Z",
    expires_at: "2026-08-13T05:00:00Z",
    resolved_at: null,
    approved_by_user_id: null,
    rejected_reason: null,
    result: null,
    request_id: "r1",
    client_ip: null,
    ...over,
  };
}

async function open(recordValue = record(), tab: "pending" | "history" = "pending") {
  vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [recordValue], total: 1, limit: 50, offset: 0 });
  const view = render(<ApprovalsView tab={tab} onTabChange={() => {}} />);
  fireEvent.click((await view.findAllByText("Restart Home Assistant"))[0]);
  await view.findByRole("dialog");
  return view;
}

describe("approval Summary modal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("opens uncluttered and mounts technical Details only on request", async () => {
    const view = await open();
    expect(await view.findByText("Home Assistant will be briefly unavailable while it restarts.")).toBeTruthy();
    expect(view.queryByText("Capability")).toBeNull();
    expect(view.container.querySelector("#approval-detail-tab-diff")).toBeNull();

    fireEvent.click(within(view.getByRole("dialog")).getByRole("button", { name: "Details" }));
    expect(await view.findByText("Capability")).toBeTruthy();
    expect(view.container.querySelector("#approval-detail-tab-diff")).toBeTruthy();
    expect(localStorage.getItem("phx-approval-default-view")).toBeNull();

    fireEvent.click(within(view.getByRole("dialog")).getByRole("button", { name: "Summary" }));
    expect(view.container.querySelector("#approval-detail-tab-diff")).toBeNull();
  });

  it("expands and focuses rejection, while allowing an empty reason", async () => {
    vi.mocked(api.rejectApproval).mockResolvedValue(record({ status: "rejected" }));
    const view = await open();
    fireEvent.click(view.getByRole("button", { name: "Reject" }));
    const input = await view.findByLabelText("Optional reason");
    expect(document.activeElement).toBe(input);
    expect(view.queryByRole("button", { name: "Approve and execute" })).toBeNull();
    expect(view.getByRole("button", { name: "Cancel" })).toBeTruthy();
    fireEvent.click(view.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(api.rejectApproval).toHaveBeenCalledWith("appr_summary", {}));
  });

  it("persists only the toolbar default", async () => {
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [record()], total: 1, limit: 50, offset: 0 });
    const view = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
    fireEvent.click(await view.findByRole("button", { name: "Details", pressed: false }));
    expect(localStorage.getItem("phx-approval-default-view")).toBe("details");
    fireEvent.click((await view.findAllByText("Restart Home Assistant"))[0]);
    expect(await view.findByText("Capability")).toBeTruthy();
  });

  it("uses friendly titles in History before the modal opens", async () => {
    const resolved = record({
      status: "approved",
      resolved_at: "2026-08-13T04:05:00Z",
      tool_name: "patch_yaml_config",
      diff: {
        kind: "yaml_diff",
        summary: "Set [11].sensor[3].state in templates.yaml",
        summary_key: "diff.patch_yaml_config.set",
        summary_params: { path: "[11].sensor[3].state", file: "templates.yaml" },
      },
    });
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [resolved], total: 1, limit: 50, offset: 0 });
    const view = render(<ApprovalsView tab="history" onTabChange={() => {}} />);
    expect(await view.findByText("Update templates.yaml")).toBeTruthy();
    expect(view.queryByText("Set [11].sensor[3].state in templates.yaml")).toBeNull();

    fireEvent.click(view.getByRole("button", { name: "Details", pressed: false }));
    expect(await view.findByText("Set [11].sensor[3].state in templates.yaml")).toBeTruthy();
    expect(view.queryByText("Update templates.yaml")).toBeNull();
  });

  it("uses friendly titles in Pending before the modal opens", async () => {
    const pending = record({
      tool_name: "patch_yaml_config",
      diff: {
        kind: "yaml_diff",
        summary: "Set [11].sensor[3].state in templates.yaml",
        summary_key: "diff.patch_yaml_config.set",
        summary_params: { path: "[11].sensor[3].state", file: "templates.yaml" },
      },
    });
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [pending], total: 1, limit: 50, offset: 0 });
    const view = render(<ApprovalsView tab="pending" onTabChange={() => {}} />);
    expect(await view.findByText("Update templates.yaml")).toBeTruthy();
    expect(view.queryByText("Set [11].sensor[3].state in templates.yaml")).toBeNull();
  });

  it.each([
    ["approved", null, null, "The request completed successfully."],
    ["rejected", "not now", null, "The request was rejected and was not run. Reason: not now"],
    ["expired", null, null, "The request expired and was not run."],
    ["cancelled", "admin_cancelled", null, "The request was cancelled and was not run. Reason: Cancelled by admin"],
    ["cancelled", "execution_failed", { tool_result: { content: [{ type: "text", text: "Forbidden." }] } }, "The request was approved, but execution failed. Error: The action could not be completed. Open Details to review the executor error."],
    ["cancelled", "execution_interrupted", null, "Execution was interrupted and the change may have been applied. Verify the current state before requesting it again."],
  ] as const)("shows a friendly %s outcome", async (status, rejectedReason, result, expected) => {
    const resolved = record({ status, rejected_reason: rejectedReason, result, resolved_at: "2026-08-13T04:05:00Z" });
    const view = await open(resolved, "history");
    expect(await view.findByText(expected)).toBeTruthy();
    expect(view.queryByText("Raw args")).toBeNull();
  });

  it.each(["pending", "history"] as const)("uses arrows to scan the visible %s list", async (tab) => {
    const first = record({
      id: `${tab}_first`,
      status: tab === "pending" ? "pending" : "approved",
      resolved_at: tab === "pending" ? null : "2026-08-13T04:05:00Z",
    });
    const second = record({
      id: `${tab}_second`,
      status: tab === "pending" ? "pending" : "approved",
      resolved_at: tab === "pending" ? null : "2026-08-13T04:06:00Z",
      tool_name: "reconfigure_zigbee_device",
      diff: {
        kind: "system_action",
        summary: "Reconfigure Zigbee device Fitness PIR ZB",
        summary_key: "diff.zigbee_reconfigure.device",
        summary_params: { label: "Fitness PIR ZB" },
      },
    });
    vi.mocked(api.listApprovals).mockResolvedValue({ approvals: [first, second], total: 2, limit: 50, offset: 0 });
    const view = render(<ApprovalsView tab={tab} onTabChange={() => {}} />);
    fireEvent.click((await view.findAllByText("Restart Home Assistant"))[0]);
    await view.findByRole("dialog");

    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(await within(view.getByRole("dialog")).findByText("Reconfigure (re-interview) Zigbee device Fitness PIR ZB")).toBeTruthy();
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(await within(view.getByRole("dialog")).findByText("Restart Home Assistant")).toBeTruthy();
  });
});
