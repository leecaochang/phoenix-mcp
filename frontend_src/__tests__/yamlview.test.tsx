import { describe, it, expect } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { YamlView, toYaml } from "../components/YamlView";

describe("YamlView", () => {
  it("renders the (none) placeholder for an empty value", () => {
    const { container } = render(<YamlView value="" />);
    expect(container.querySelector("pre.yaml-pre-empty")?.textContent).toBe("(none)");
  });

  it("falls back to <pre> then upgrades to ha-code-editor when HA defines it", async () => {
    // ha-code-editor is not registered in jsdom at mount: styled <pre> fallback.
    const { container } = render(<YamlView value={"a: 1"} />);
    expect(container.querySelector("pre.yaml-pre")).toBeTruthy();
    expect(container.querySelector("ha-code-editor")).toBeNull();

    // HA lazy-registers the element later; YamlView must upgrade to it (this is
    // the regression fix: it no longer decides once at mount).
    if (!customElements.get("ha-code-editor")) {
      customElements.define("ha-code-editor", class extends HTMLElement {});
    }
    await waitFor(() => {
      expect(container.querySelector("ha-code-editor")).toBeTruthy();
    });
  });
});

describe("toYaml", () => {
  it("serialises an object to block YAML in insertion order", () => {
    expect(toYaml({ alias: "Morning", mode: "single" })).toBe("alias: Morning\nmode: single\n");
  });
  it("returns empty string for null", () => {
    expect(toYaml(null)).toBe("");
  });
});
