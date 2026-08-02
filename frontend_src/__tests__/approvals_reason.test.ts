import { describe, it, expect } from "vitest";
import type { ApprovalRecord } from "../types";
import { extractResultErrorText, friendlyReason } from "../views/ApprovalsView";

function rec(over: Partial<ApprovalRecord>): ApprovalRecord {
  return {
    id: "appr_1",
    token_id: "t1",
    token_name: "my_token",
    tool_name: "call_service_mesa_approved",
    cap_name: "mesa_control_mode",
    args: {},
    diff: {},
    status: "rejected",
    created_at: "",
    expires_at: "",
    resolved_at: null,
    approved_by_user_id: null,
    rejected_reason: null,
    result: null,
    request_id: "r1",
    client_ip: null,
    ...over,
  };
}

describe("extractResultErrorText", () => {
  it("pulls the tool error text out of a saved result", () => {
    const result = {
      tool_result: { content: [{ type: "text", text: "Forbidden." }], isError: true },
      outcome: "denied",
    };
    expect(extractResultErrorText(result)).toBe("Forbidden.");
  });

  it("returns null when there is no usable text", () => {
    expect(extractResultErrorText(null)).toBeNull();
    expect(extractResultErrorText({})).toBeNull();
    expect(extractResultErrorText({ tool_result: { content: [] } })).toBeNull();
  });
});

describe("friendlyReason", () => {
  it("replaces the execution_failed slug with the underlying tool error", () => {
    const r = rec({
      rejected_reason: "execution_failed",
      result: { tool_result: { content: [{ type: "text", text: "Forbidden." }], isError: true } },
    });
    expect(friendlyReason(r)).toBe("Execution failed: Forbidden.");
  });

  it("falls back to a readable label when execution_failed has no result text", () => {
    const r = rec({ rejected_reason: "execution_failed", result: null });
    expect(friendlyReason(r)).toBe("Execution failed");
  });

  it("maps known reason slugs to readable labels", () => {
    expect(friendlyReason(rec({ rejected_reason: "capability_denied" }))).toBe("Capability was denied");
    expect(friendlyReason(rec({ rejected_reason: "kill_switch" }))).toBe("Kill switch engaged");
  });

  it("passes a free-text admin reason through verbatim", () => {
    expect(friendlyReason(rec({ rejected_reason: "you can't do this!" }))).toBe("you can't do this!");
  });

  it("returns an empty string when there is no reason", () => {
    expect(friendlyReason(rec({ rejected_reason: null }))).toBe("");
  });
});
