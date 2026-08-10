import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import type { EntityTree as EntityTreeData, PermissionTree } from "../types";

const mocks = vi.hoisted(() => ({
  getEntityTree: vi.fn(),
  getEntityHints: vi.fn(),
  patchDevicePermission: vi.fn(),
}));

vi.mock("../api", () => ({
  api: {
    getEntityTree: mocks.getEntityTree,
    getEntityHints: mocks.getEntityHints,
    patchDevicePermission: mocks.patchDevicePermission,
  },
}));

import { EntityTree, matchesTreeSearch } from "../components/EntityTree";

const DEVICE_ID = "93d485bc30d4dc83aaf5efed8ea23fd3";
const GHOST_DEVICE_ID = "deadbeefdeadbeefdeadbeefdeadbeef";

const TREE: EntityTreeData = {
  binary_sensor: {
    devices: {
      [DEVICE_ID]: {
        device_id: DEVICE_ID,
        name: "Phoenix Removal Smoke 1.1.1.1",
        area_id: null,
        area_name: null,
        entities: ["binary_sensor.phoenix_removal_smoke_1_1_1_1"],
      },
    },
    deviceless_entities: [],
    entity_details: {
      "binary_sensor.phoenix_removal_smoke_1_1_1_1": {
        entity_id: "binary_sensor.phoenix_removal_smoke_1_1_1_1",
        friendly_name: "Phoenix Removal Smoke status",
        device_id: DEVICE_ID,
        area_id: null,
        area_name: null,
        labels: [],
      },
    },
  },
};

const EMPTY_PERMISSIONS: PermissionTree = { domains: {}, devices: {}, entities: {} };

function renderTree(permissions: PermissionTree = EMPTY_PERMISSIONS) {
  const onPermissionsChange = vi.fn();
  render(
    <EntityTree
      tokenId="token-1"
      permissions={permissions}
      onPermissionsChange={onPermissionsChange}
    />,
  );
  return { onPermissionsChange };
}

describe("permission tree search and device cleanup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getEntityTree.mockResolvedValue(TREE);
    mocks.getEntityHints.mockResolvedValue({ entity_hints: {} });
    mocks.patchDevicePermission.mockResolvedValue(EMPTY_PERMISSIONS);
  });

  it("matches normalized punctuation variants without losing literal matching", () => {
    expect(matchesTreeSearch("1.1.1.1", "binary_sensor.host_1_1_1_1")).toBe(true);
    expect(matchesTreeSearch("REMOVAL SMOKE", "Phoenix Removal Smoke")).toBe(true);
    expect(matchesTreeSearch("garage", DEVICE_ID, "Phoenix Removal Smoke")).toBe(false);
  });

  it("finds a device by raw registry ID through its domain ancestor", async () => {
    renderTree();
    const filter = await screen.findByRole("textbox", { name: "Filter domains, devices, and entities" });

    fireEvent.change(filter, { target: { value: DEVICE_ID } });

    expect(await screen.findByText("Phoenix Removal Smoke 1.1.1.1")).toBeInTheDocument();
    expect(screen.getByText(DEVICE_ID).tagName).toBe("CODE");
    expect(screen.queryByRole("button", { name: /copy device id/i })).not.toBeInTheDocument();
  });

  it("finds an underscore-derived entity ID when the operator types the dotted address", async () => {
    renderTree();
    const filter = await screen.findByRole("textbox", { name: "Filter domains, devices, and entities" });

    fireEvent.change(filter, { target: { value: "1.1.1.1" } });

    expect(await screen.findByText("binary_sensor.phoenix_removal_smoke_1_1_1_1")).toBeInTheDocument();
  });

  it("restores a collapsed domain after clearing a search", async () => {
    renderTree();
    const filter = await screen.findByRole("textbox", { name: "Filter domains, devices, and entities" });
    expect(screen.getByRole("button", { name: "Expand binary_sensor" })).toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "1.1.1.1" } });
    expect(await screen.findByRole("button", { name: "Collapse binary_sensor" })).toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "" } });
    expect(await screen.findByRole("button", { name: "Expand binary_sensor" })).toBeInTheDocument();
  });

  it("restores independent domain and device choices after clearing a search", async () => {
    renderTree();
    const filter = await screen.findByRole("textbox", { name: "Filter domains, devices, and entities" });
    fireEvent.click(screen.getByRole("button", { name: "Expand binary_sensor" }));
    expect(await screen.findByRole("button", { name: "Expand Phoenix Removal Smoke 1.1.1.1" })).toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "1.1.1.1" } });
    expect(await screen.findByRole("button", { name: "Collapse Phoenix Removal Smoke 1.1.1.1" })).toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "" } });
    expect(await screen.findByRole("button", { name: "Collapse binary_sensor" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand Phoenix Removal Smoke 1.1.1.1" })).toBeInTheDocument();
  });

  it("surfaces an unmatched explicit device grant and lets the operator clear it", async () => {
    const permissions: PermissionTree = {
      domains: {},
      devices: { [GHOST_DEVICE_ID]: { state: "GREEN", hint: null } },
      entities: {},
    };
    const { onPermissionsChange } = renderTree(permissions);

    expect(await screen.findByText("Unmatched device permissions")).toBeInTheDocument();
    expect(screen.getByText(GHOST_DEVICE_ID)).toBeInTheDocument();
    const selector = screen.getByRole("group", { name: `Permission for device ${GHOST_DEVICE_ID}` });
    const clearButton = within(selector).getByRole("button", {
      name: "No explicit grant (inherits from parent)",
    });
    clearButton.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(clearButton, { pointerId: 1 });

    await waitFor(() => expect(mocks.patchDevicePermission).toHaveBeenCalledWith(
      "token-1", GHOST_DEVICE_ID, { state: "GREY" },
    ));
    expect(onPermissionsChange).toHaveBeenCalledWith(EMPTY_PERMISSIONS);
  });

  it("shows a useful empty state instead of a blank tree", async () => {
    renderTree();
    const filter = await screen.findByRole("textbox", { name: "Filter domains, devices, and entities" });

    fireEvent.change(filter, { target: { value: "does-not-exist" } });

    expect(screen.getByRole("status")).toHaveTextContent(
      "No domains, devices, entities, or stored grants match this filter.",
    );
  });
});
