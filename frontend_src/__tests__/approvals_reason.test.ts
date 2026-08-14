import { afterEach, describe, it, expect } from "vitest";
import type { ApprovalRecord } from "../types";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import ja from "../../custom_components/phoenix_mcp/catalogs/ja.json";
import zh from "../../custom_components/phoenix_mcp/catalogs/zh-Hans.json";
import { primeTranslations } from "../i18n";
import { extractResultErrorText, friendlyReason, localizedResultErrorText } from "../views/ApprovalsView";

afterEach(() => primeTranslations(en.panel));

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

  it("extracts the short error from a legacy structured integration result", () => {
    const text = JSON.stringify({
      error: "MESA blocked the integration action.",
      entry_id: "legacy_ping",
      mesa: { entities: Array.from({ length: 20 }, (_, i) => ({
        entity_id: `sensor.legacy_${i}`,
        effective_rule: { explanation: "verbose diagnostic data".repeat(20) },
      })) },
    });
    expect(extractResultErrorText({
      tool_result: { content: [{ type: "text", text }], isError: true },
    })).toBe("MESA blocked the integration action.");
  });

  it("caps malformed legacy error text instead of breaking Summary", () => {
    const extracted = extractResultErrorText({
      tool_result: { content: [{ type: "text", text: "x".repeat(1000) }], isError: true },
    });
    expect(extracted).toHaveLength(280);
    expect(extracted?.endsWith("...")).toBe(true);
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
    expect(friendlyReason(r)).toBe("Execution failed: The action could not be completed. Open Details to review the executor error.");
  });

  it("falls back to a readable label when execution_failed has no result text", () => {
    const r = rec({ rejected_reason: "execution_failed", result: null });
    expect(friendlyReason(r)).toBe("Execution failed");
  });

  it("maps known reason slugs to readable labels", () => {
    expect(friendlyReason(rec({ rejected_reason: "capability_denied" }))).toBe("Capability was denied");
    expect(friendlyReason(rec({ rejected_reason: "kill_switch" }))).toBe("Kill switch engaged");
    expect(friendlyReason(rec({ rejected_reason: "agent_chat_ended" }))).toBe("Agent Chat ended before approval");
  });

  it("passes a free-text admin reason through verbatim", () => {
    expect(friendlyReason(rec({ rejected_reason: "you can't do this!" }))).toBe("you can't do this!");
  });

  it("returns an empty string when there is no reason", () => {
    expect(friendlyReason(rec({ rejected_reason: null }))).toBe("");
  });
});

describe("localizedResultErrorText", () => {
  it("localizes the stable integration revalidation error", () => {
    primeTranslations(ja.panel);
    const error = "The integration's state, resource membership, permissions, or effective MESA profile changed after approval. Review it again.";
    expect(localizedResultErrorText({
      tool_result: { content: [{ type: "text", text: error }], isError: true },
    })).toBe("承認後にインテグレーションの状態または権限が変更されました。現在のリクエストの詳細を確認して、もう一度お試しください。");
  });

  it("uses a localized fallback instead of leaking an unknown diagnostic", () => {
    expect(localizedResultErrorText({
      tool_result: { content: [{ type: "text", text: "A new diagnostic." }], isError: true },
    })).toBe("The action could not be completed. Open Details to review the executor error.");
  });

  it("localizes a disabled integration reload in Japanese and Chinese", () => {
    const result = {
      tool_result: { content: [{ type: "text", text: "Disabled integrations cannot be reloaded." }], isError: true },
    };
    primeTranslations(ja.panel);
    expect(localizedResultErrorText(result)).toBe("インテグレーションが無効になっているため、再読み込みできません。");
    expect(friendlyReason(rec({ rejected_reason: "execution_failed", result }))).toBe(
      "実行に失敗しました: インテグレーションが無効になっているため、再読み込みできません。",
    );
    primeTranslations(zh.panel);
    expect(localizedResultErrorText(result)).toBe("该集成已被禁用，无法重新加载。");
  });

  it("keeps unknown executor errors localized in every shipped non-English locale", () => {
    const result = {
      tool_result: { content: [{ type: "text", text: "A future Home Assistant diagnostic." }], isError: true },
    };
    primeTranslations(ja.panel);
    expect(localizedResultErrorText(result)).toBe("操作を完了できませんでした。実行エラーを確認するには、詳細を開いてください。");
    primeTranslations(zh.panel);
    expect(localizedResultErrorText(result)).toBe("无法完成此操作。请打开详细信息查看执行器错误。");
  });

  it.each([
    "Zigbee2MQTT rejected the device/configure request: Failed to configure (Bind failed)",
    "Home Assistant rejected the Energy configuration: Invalid arguments for energy/save_prefs",
    "Failed to delete blueprint: WebSocket command blueprint/delete failed: Blueprint in use",
  ])("keeps external executor diagnostic out of the Japanese friendly result: %s", (diagnostic) => {
    const result = {
      tool_result: { content: [{ type: "text", text: diagnostic }], isError: true },
    };
    primeTranslations(ja.panel);
    expect(localizedResultErrorText(result)).toBe(
      "操作を完了できませんでした。実行エラーを確認するには、詳細を開いてください。",
    );
    expect(friendlyReason(rec({ rejected_reason: "execution_failed", result }))).not.toContain(diagnostic);
  });

  it("localizes a dynamic non-reloadable integration state", () => {
    const result = {
      tool_result: { content: [{ type: "text", text: "Integration state setup_in_progress is not reloadable." }], isError: true },
    };
    primeTranslations(ja.panel);
    expect(localizedResultErrorText(result)).toBe("インテグレーションは現在「setup_in_progress」の状態のため、再読み込みできません。");
  });
});
