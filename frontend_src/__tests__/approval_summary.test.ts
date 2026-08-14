import { afterEach, describe, expect, it, vi } from "vitest";
import {
  friendlyApprovalSummary,
  rememberApprovalView,
  storedApprovalView,
} from "../utils/approval_summary";

describe("friendly approval summary", () => {
  it.each([
    [
      "diff.zigbee_reconfigure.device",
      { label: "Fitness PIR ZB" },
      "Reconfigure (re-interview) Zigbee device Fitness PIR ZB",
      "This will interview Fitness PIR ZB again. It may be briefly unresponsive. Battery devices must be awake.",
    ],
    [
      "diff.integration.reload",
      { label: "ping (192.0.2.10)" },
      "Reload integration ping (192.0.2.10)",
      "This will temporarily unload the ping (192.0.2.10) integration and set it up again.",
    ],
    [
      "diff.restart_ha",
      {},
      "Restart Home Assistant",
      "Home Assistant will be briefly unavailable while it restarts.",
    ],
    [
      "diff.patch_yaml_config.set",
      { path: "[11].sensor[3].state", file: "templates.yaml" },
      "Update templates.yaml",
      "Open Details to review the exact proposed change.",
    ],
    [
      "diff.dashboard_card.edit.section",
      { card_index: 4, label: "dashboard-steve", view_index: 0, section_index: 5 },
      "Update a card on dashboard 'dashboard-steve'",
      "This will update the Home Assistant dashboard configuration. Open Details to review the exact proposed change.",
    ],
  ])("renders %s without parsing YAML", (summaryKey, summaryParams, title, body) => {
    expect(friendlyApprovalSummary({ summary_key: summaryKey, summary_params: summaryParams })).toEqual({ title, body });
  });

  it.each([
    [{ target: { type: "file", id: "/config/templates.yaml" } }, "Update templates.yaml"],
    [{ target: { type: "integration", label: "ping" } }, "Update integration ping"],
    [{ target: { type: "device", label: "Fitness PIR ZB" } }, "Update Fitness PIR ZB"],
    [{ target: { type: "service", label: "light.turn_on" } }, "Run light.turn_on"],
    [{ summary_key: "diff.from_a_newer_backend" }, "Review proposed action"],
    [{}, "Review proposed action"],
  ])("uses a deterministic fallback for old and unknown records", (diff, title) => {
    expect(friendlyApprovalSummary(diff).title).toBe(title);
  });

  it("uses an automation's saved label instead of its numeric ID", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.edit_automation",
      summary_params: { automation_id: "5739529167413" },
      target: { type: "automation", id: "5739529167413", label: "Turn on Lights at Sunset" },
    }).title).toBe("Edit automation 'Turn on Lights at Sunset'");
  });

  it("uses the saved entity label for registry metadata changes", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.set_entity",
      summary_params: { fields: "aliases", entity_id: "automation.test" },
      target: { type: "entity", id: "automation.test", label: "~test" },
    }).title).toBe("Update ~test");
  });

  it("recovers the friendly name from an older alias-change preview", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.set_entity",
      summary_params: { fields: "aliases", entity_id: "automation.test" },
      target: { type: "entity", id: "automation.test", label: "automation.test" },
      preview: { aliases: { before: ["~test"], after: ["~test", "Smoke test"] } },
    }).title).toBe("Update ~test");
  });

  it("names a removed device and its integration instead of exposing registry IDs", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.remove_device",
      summary_params: {
        device_id: "aec2db8398267dfd1100a3816e6eea8e",
        config_entry_id: "01JDVFKNKF1SYGTFFWTFVC6NZC",
      },
      target: {
        type: "device",
        id: "aec2db8398267dfd1100a3816e6eea8e",
        label: "Kitchen motion sensor",
      },
      preview: {
        selected_owner: { title: "Zigbee2MQTT", domain: "mqtt" },
      },
    })).toEqual({
      title: "Remove Kitchen motion sensor from Zigbee2MQTT",
      body: "This will ask the Zigbee2MQTT integration to remove Kitchen motion sensor. You may need to add the device again, and this action cannot be undone from Phoenix MCP.",
    });
  });

  it("hides opaque device IDs when an old removal record has no saved name", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.remove_device",
      target: {
        type: "device",
        id: "aec2db8398267dfd1100a3816e6eea8e",
        label: "aec2db8398267dfd1100a3816e6eea8e",
      },
      preview: { selected_owner: { domain: "mqtt" } },
    }).title).toBe("Remove device from mqtt");
  });

  it("uses an automation name for deletion", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.delete_automation",
      summary_params: { automation_id: "1734362394008" },
      target: { type: "automation", id: "1734362394008", label: "~test" },
    })).toEqual({
      title: "Delete automation '~test'",
      body: "This will permanently delete automation '~test'. This action cannot be undone.",
    });
  });

  it("uses a script alias instead of its storage slug", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.create_script",
      summary_params: { script_id: "approval_summary_smoke_test" },
      target: { type: "script", id: "approval_summary_smoke_test", label: "Approval Summary Smoke Test" },
    })).toEqual({
      title: "Create script 'Approval Summary Smoke Test'",
      body: "This will add script 'Approval Summary Smoke Test' to Home Assistant. Open Details to review its configuration.",
    });
  });

  it("does not present a legacy script storage slug as a friendly name", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.delete_script",
      summary_params: { script_id: "approval_summary_smoke_test" },
      target: {
        type: "script",
        id: "approval_summary_smoke_test",
        label: "approval_summary_smoke_test",
      },
    })).toEqual({
      title: "Delete script",
      body: "This will permanently delete the selected script. This action cannot be undone.",
    });
  });

  it("does not present a legacy helper storage slug as a friendly name", () => {
    expect(friendlyApprovalSummary({
      summary_key: "diff.delete_helper",
      summary_params: {
        helper_type: "input_boolean",
        helper_id: "approval_smoke_test_helper",
      },
      target: {
        type: "helper",
        id: "approval_smoke_test_helper",
        label: "approval_smoke_test_helper",
      },
    })).toEqual({
      title: "Delete input boolean helper",
      body: "This will permanently delete the selected input boolean helper. This action cannot be undone.",
    });
  });
});

describe("approval Summary preference", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("defaults missing and invalid values to Summary", () => {
    localStorage.clear();
    expect(storedApprovalView()).toBe("summary");
    localStorage.setItem("phx-approval-default-view", "preview");
    expect(storedApprovalView()).toBe("summary");
  });

  it("stores Details per browser", () => {
    rememberApprovalView("details");
    expect(storedApprovalView()).toBe("details");
  });

  it("defaults to Summary when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(storedApprovalView()).toBe("summary");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => rememberApprovalView("details")).not.toThrow();
  });
});
