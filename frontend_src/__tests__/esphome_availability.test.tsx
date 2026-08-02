import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../api", () => ({ api: {} }));

import { CapabilityMatrix } from "../components/CapabilityMatrix";
import { PersonaPicker } from "../components/PersonaPicker";
import type { TokenRecord } from "../types";

function token(over: Partial<TokenRecord> = {}): TokenRecord {
  return {
    id: "tok-1",
    name: "t",
    persona: "custom",
    pass_through: false,
    cap_esphome_yaml: "deny",
    cap_esphome_flash: "deny",
    ...(over as object),
  } as unknown as TokenRecord;
}

// Both ESPHome capabilities carry the badge, so each message appears once per row.
const ESPHOME_CAP_ROWS = 2;

describe("ESPHome availability in the panel", () => {
  it("marks the capabilities when neither ESPHome nor the add-on is present", () => {
    render(
      <CapabilityMatrix
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: false, builder: false }}
      />,
    );
    expect(screen.getAllByText("ESPHome not detected")).toHaveLength(ESPHOME_CAP_ROWS);
  });

  it("names the add-on specifically when ESPHome itself is present", () => {
    render(
      <CapabilityMatrix
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: true, builder: false }}
      />,
    );
    expect(screen.getAllByText("Device Builder add-on not detected"))
      .toHaveLength(ESPHOME_CAP_ROWS);
  });

  it("shows nothing when the Device Builder is available", () => {
    render(
      <CapabilityMatrix
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: true, builder: true }}
      />,
    );
    expect(screen.queryByText(/not detected/)).toBeNull();
  });

  it("shows nothing while settings are still loading", () => {
    render(<CapabilityMatrix token={token()} onUpdate={vi.fn()} esphome={null} />);
    expect(screen.queryByText(/not detected/)).toBeNull();
  });

  it("keeps the capability settable so a token can be prepared in advance", () => {
    render(
      <CapabilityMatrix
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: false, builder: false }}
      />,
    );
    const group = screen.getByRole("radiogroup", { name: "ESPHome device YAML" });
    const radios = group.querySelectorAll("input");
    expect(radios.length).toBeGreaterThan(0);
    // Only the Confirm-unavailable mechanic may disable a mode; absence of
    // ESPHome must not.
    expect(Array.from(radios).every((r) => !(r as HTMLInputElement).disabled)).toBe(true);
  });

  it("notes the missing system on the esphome persona card", () => {
    render(
      <PersonaPicker
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: false, builder: false }}
      />,
    );
    expect(screen.getByText("ESPHome was not detected on this system")).toBeTruthy();
  });

  it("leaves the persona card unannotated when ESPHome is present", () => {
    render(
      <PersonaPicker
        token={token()}
        onUpdate={vi.fn()}
        esphome={{ integration: true, builder: true }}
      />,
    );
    expect(screen.queryByText(/was not detected/)).toBeNull();
  });
});
