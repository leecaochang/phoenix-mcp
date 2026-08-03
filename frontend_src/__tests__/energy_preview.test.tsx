import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EnergyPreview } from "../views/ApprovalsView";

/**
 * The visual Preview for an Energy approval.
 *
 * Energy has no Lovelace layout, so the hui-card preview the dashboard tools use
 * has nothing to render, and HA's own energy cards read preferences from the
 * backend collection rather than from card config: one embedded here would show
 * the CURRENT dashboard regardless of what the approval proposes, which is worse
 * than no picture. This renders the configuration itself.
 *
 * The property worth pinning is the KEYING. Rows pair by statistic (devices) and
 * by type (sources), never by display name, so a rename reads as one substitution
 * rather than an unrelated removal plus addition. That is the whole reason an
 * operator would rather look at this than at eighty lines of JSON where one
 * string differs.
 */

const SOURCES = [
  { type: "grid", meters: [["stat_energy_from", "sensor.grid_import"]] as Array<[string, string]> },
  { type: "solar", meters: [["stat_energy_from", "sensor.solar"]] as Array<[string, string]> },
];

function rowsByClass(cls: string): string[] {
  return Array.from(document.querySelectorAll(`.energy-preview-row.is-${cls}`)).map(
    (el) => el.querySelector(".energy-preview-name")?.textContent ?? "",
  );
}

describe("EnergyPreview", () => {
  it("renders sources and devices with nothing marked when nothing changed", () => {
    const rows = {
      sources: SOURCES,
      devices: [{ name: "Washer", statistic: "sensor.washer_energy" }],
    };
    render(<EnergyPreview energy={{ before: rows, after: rows }} />);
    expect(rowsByClass("same")).toEqual(["Grid", "Solar", "Washer"]);
    expect(rowsByClass("added")).toEqual([]);
    expect(rowsByClass("removed")).toEqual([]);
  });

  it("a rename is ONE substitution, not a removal plus an unrelated addition", () => {
    const before = { sources: [], devices: [{ name: "Treadmill", statistic: "sensor.tread" }] };
    const after = { sources: [], devices: [{ name: "Treadmill (new)", statistic: "sensor.tread" }] };
    render(<EnergyPreview energy={{ before, after }} />);
    // Old value first, new value directly beneath, so the substitution reads.
    expect(rowsByClass("changed")).toEqual(["Treadmill"]);
    expect(rowsByClass("added")).toEqual(["Treadmill (new)"]);
    expect(rowsByClass("same")).toEqual([]);
  });

  it("repointing a device at a different statistic reads as a replacement", () => {
    const before = { devices: [{ name: "Washer", statistic: "sensor.old_washer" }] };
    const after = { devices: [{ name: "Washer", statistic: "sensor.new_washer" }] };
    render(<EnergyPreview energy={{ before, after }} />);
    // Keyed by statistic, so this is genuinely a different row, not an edit.
    expect(rowsByClass("removed")).toEqual(["Washer"]);
    expect(rowsByClass("added")).toEqual(["Washer"]);
    expect(screen.getByText("sensor.old_washer")).toBeTruthy();
    expect(screen.getByText("sensor.new_washer")).toBeTruthy();
  });

  it("marks an added device and a removed source", () => {
    render(
      <EnergyPreview
        energy={{
          before: { sources: SOURCES, devices: [] },
          after: { sources: [SOURCES[0]], devices: [{ name: "Dryer", statistic: "sensor.dryer" }] },
        }}
      />,
    );
    expect(rowsByClass("removed")).toEqual(["Solar"]);
    expect(rowsByClass("added")).toEqual(["Dryer"]);
    expect(rowsByClass("same")).toEqual(["Grid"]);
  });

  it("resolves the source type through the catalog, never as a raw slug", () => {
    render(<EnergyPreview energy={{ before: { sources: SOURCES }, after: { sources: SOURCES } }} />);
    // "grid"/"solar" are server-side enum values; rendering them raw would stay
    // English in every locale and drag ASCII spacing into Chinese with them.
    expect(screen.queryByText("grid")).toBeNull();
    expect(screen.getByText("Grid")).toBeTruthy();
  });

  it("survives an empty configuration and missing keys", () => {
    render(<EnergyPreview energy={{}} />);
    expect(document.querySelectorAll(".energy-preview-empty").length).toBe(2);
  });
});
