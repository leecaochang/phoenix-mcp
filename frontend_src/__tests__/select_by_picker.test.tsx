import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { EntityTree } from "../types";

const mocks = vi.hoisted(() => ({
  getPermissionIntegrationOptions: vi.fn(),
  bulkSelectPermissions: vi.fn(),
}));

vi.mock("../api", () => ({
  api: {
    getPermissionIntegrationOptions: mocks.getPermissionIntegrationOptions,
    bulkSelectPermissions: mocks.bulkSelectPermissions,
  },
}));

import { SelectByPicker } from "../components/SelectByPicker";

const TREE: EntityTree = {
  light: {
    devices: {},
    deviceless_entities: ["light.kitchen"],
    entity_details: {
      "light.kitchen": {
        entity_id: "light.kitchen",
        friendly_name: "Kitchen",
        device_id: null,
        area_id: "kitchen",
        area_name: "Kitchen",
        labels: [{ id: "main", name: "Main" }],
      },
    },
  },
};

const OPTION = {
  entry_id: "0123456789abcdef0123456789abcdef",
  domain: "ping",
  title: "Kitchen host",
  device_count: 2,
  deviceless_entity_count: 1,
  registry_only_deviceless_count: 2,
  required_domain_ids: ["binary_sensor", "sensor"],
  shared_device_count: 1,
};

describe("SelectByPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getPermissionIntegrationOptions.mockResolvedValue({ integrations: [OPTION] });
    mocks.bulkSelectPermissions.mockResolvedValue({
      permissions: { domains: {}, devices: {}, entities: {} },
      summary: {
        selector_type: "integration",
        selector_id: OPTION.entry_id,
        state: "GREEN",
        device_count: 2,
        entity_count: 1,
        registry_only_deviceless_count: 2,
        required_domain_ids: ["binary_sensor", "sensor"],
        shared_device_count: 1,
      },
    });
  });

  it("shows exact entry identity and coverage warnings without a copy control", async () => {
    render(<SelectByPicker tokenId="token-1" entityTree={TREE} onDone={() => {}} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Integration" }));
    const picker = await screen.findByRole("combobox", { name: "Integration" });
    fireEvent.change(picker, { target: { value: OPTION.entry_id } });

    const id = screen.getByText(OPTION.entry_id);
    expect(id.tagName).toBe("CODE");
    expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
    expect(screen.getByText(/2 registry-only deviceless entities/)).toBeInTheDocument();
    expect(screen.getByText(/1 selected device is shared/)).toBeInTheDocument();
    expect(screen.getByText(/binary_sensor, sensor/)).toBeInTheDocument();
  });

  it("applies the integration selection through one atomic request", async () => {
    const onDone = vi.fn();
    render(<SelectByPicker tokenId="token-1" entityTree={TREE} onDone={onDone} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Integration" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Integration" }), {
      target: { value: OPTION.entry_id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(mocks.bulkSelectPermissions).toHaveBeenCalledOnce());
    expect(mocks.bulkSelectPermissions).toHaveBeenCalledWith(
      "token-1", "integration", OPTION.entry_id, "GREEN",
    );
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("keeps area selection on the same atomic endpoint", async () => {
    render(<SelectByPicker tokenId="token-1" entityTree={TREE} onDone={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByRole("combobox", { name: "Area" }), {
      target: { value: "kitchen" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(mocks.bulkSelectPermissions).toHaveBeenCalledWith(
      "token-1", "area", "kitchen", "GREEN",
    ));
  });
});
