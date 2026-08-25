import React from "react";
import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { collectConfigErrors, collectPreviewViews, DashboardPreview, singleCardPreviewConfig } from "../components/DashboardPreview";

describe("collectPreviewViews", () => {
  it("returns null for non-dict configs", () => {
    expect(collectPreviewViews(null)).toBeNull();
    expect(collectPreviewViews("x")).toBeNull();
    expect(collectPreviewViews(42)).toBeNull();
    expect(collectPreviewViews([{ views: [] }])).toBeNull();
  });

  it("returns null when views is missing or not a list", () => {
    expect(collectPreviewViews({})).toBeNull();
    expect(collectPreviewViews({ views: {} })).toBeNull();
    expect(collectPreviewViews({ views: "nope" })).toBeNull();
  });

  it("returns null for a strategy dashboard (views only exist at runtime)", () => {
    expect(collectPreviewViews({ strategy: { type: "original-states" }, views: [] })).toBeNull();
  });

  it("returns an empty list for an empty views list", () => {
    expect(collectPreviewViews({ views: [] })).toEqual([]);
  });

  it("turns a plain view's cards into one untitled section, preserving card dicts by reference", () => {
    const card = { type: "light", entity: "light.desk" };
    const views = collectPreviewViews({ views: [{ title: "Home", cards: [card] }] })!;
    expect(views).toHaveLength(1);
    expect(views[0].kind).toBe("cards");
    expect(views[0].title).toBe("Home");
    expect(views[0].sections).toHaveLength(1);
    expect(views[0].sections[0].title).toBeNull();
    expect(views[0].sections[0].cards).toEqual([{ kind: "card", config: card }]);
    expect((views[0].sections[0].cards[0] as { config: unknown }).config).toBe(card);
  });

  it("flags a non-dict card entry as invalid without dropping it", () => {
    const views = collectPreviewViews({ views: [{ cards: [{ type: "markdown" }, "garbage", 7] }] })!;
    expect(views[0].sections[0].cards.map((c) => c.kind)).toEqual(["card", "invalid", "invalid"]);
  });

  it("groups a sections view per section with titles, skipping non-dict sections", () => {
    const views = collectPreviewViews({
      views: [{
        type: "sections",
        sections: [
          { title: "Lights", cards: [{ type: "light" }] },
          "garbage",
          { title: "", cards: [{ type: "sensor" }] },
        ],
      }],
    })!;
    expect(views[0].kind).toBe("cards");
    expect(views[0].sections).toHaveLength(2);
    expect(views[0].sections[0].title).toBe("Lights");
    expect(views[0].sections[1].title).toBeNull();
  });

  it("concatenates plain cards before sections when a view carries both", () => {
    const views = collectPreviewViews({
      views: [{
        cards: [{ type: "button" }],
        sections: [{ title: "S", cards: [{ type: "light" }] }],
      }],
    })!;
    expect(views[0].sections).toHaveLength(2);
    expect(views[0].sections[0].title).toBeNull();
    expect(views[0].sections[1].title).toBe("S");
  });

  it("marks a strategy view as strategy with no sections", () => {
    const views = collectPreviewViews({ views: [{ title: "Auto", strategy: { type: "area" } }] })!;
    expect(views[0]).toEqual({ title: "Auto", kind: "strategy", sections: [] });
  });

  it("marks a non-dict view entry as invalid, keeping the tab count", () => {
    const views = collectPreviewViews({ views: [{ cards: [{ type: "light" }] }, "garbage"] })!;
    expect(views).toHaveLength(2);
    expect(views[1]).toEqual({ title: "View 2", kind: "invalid", sections: [] });
  });

  it("marks a view with no renderable cards as empty", () => {
    const views = collectPreviewViews({ views: [{ title: "Bare" }, { cards: [] }, { sections: [{ cards: [] }] }] })!;
    expect(views.map((v) => v.kind)).toEqual(["empty", "empty", "empty"]);
  });

  it("falls back through title, path, then a positional name", () => {
    const views = collectPreviewViews({
      views: [{ title: "T", cards: [] }, { path: "p", cards: [] }, { title: "", path: "", cards: [] }],
    })!;
    expect(views.map((v) => v.title)).toEqual(["T", "p", "View 3"]);
  });
});

describe("singleCardPreviewConfig", () => {
  it("returns null for null, undefined, or empty input", () => {
    expect(singleCardPreviewConfig(null)).toBeNull();
    expect(singleCardPreviewConfig(undefined)).toBeNull();
    expect(singleCardPreviewConfig("")).toBeNull();
  });

  it("returns null for unparseable JSON (e.g. a truncated string)", () => {
    expect(singleCardPreviewConfig('{"type": "markdown", "conte')).toBeNull();
  });

  it("returns null when the parsed value is not an object", () => {
    expect(singleCardPreviewConfig("42")).toBeNull();
    expect(singleCardPreviewConfig("[1,2,3]")).toBeNull();
    expect(singleCardPreviewConfig('"just a string"')).toBeNull();
  });

  it("wraps a valid card into a one-card, one-view dashboard config", () => {
    const wrapped = singleCardPreviewConfig('{"type":"markdown","content":"hi"}');
    expect(wrapped).toEqual({ views: [{ cards: [{ type: "markdown", content: "hi" }] }] });
    // The wrapped config must itself be previewable by collectPreviewViews.
    const views = collectPreviewViews(wrapped)!;
    expect(views).toHaveLength(1);
    expect(views[0].kind).toBe("cards");
    expect(views[0].sections[0].cards).toEqual([{ kind: "card", config: { type: "markdown", content: "hi" } }]);
  });
});

describe("collectConfigErrors", () => {
  function errorCard(config?: { error?: unknown; message?: unknown }, text?: string): HTMLElement {
    const el = document.createElement("hui-error-card");
    if (config) (el as unknown as { _config: unknown })._config = config;
    if (text) el.textContent = text;
    return el;
  }

  it("returns [] for a tree with no error cards", () => {
    const host = document.createElement("div");
    host.appendChild(document.createElement("ha-card"));
    expect(collectConfigErrors(host)).toEqual([]);
  });

  it("extracts the config error and message from a light-DOM error card", () => {
    const host = document.createElement("div");
    host.appendChild(errorCard({ error: "value.series[0] is not valid", message: "check the docs" }));
    expect(collectConfigErrors(host)).toEqual(["value.series[0] is not valid - check the docs"]);
  });

  it("falls back to rendered text when the error card has no config", () => {
    const host = document.createElement("div");
    host.appendChild(errorCard(undefined, "Configuration error: boom"));
    expect(collectConfigErrors(host)).toEqual(["Configuration error: boom"]);
  });

  it("falls back to a generic message when there is no config and no text", () => {
    const host = document.createElement("div");
    host.appendChild(errorCard());
    expect(collectConfigErrors(host)).toEqual(["Configuration error (no detail available)"]);
  });

  it("finds an error card nested inside an open shadow root", () => {
    const host = document.createElement("div");
    const wrapper = document.createElement("some-card");
    const shadow = wrapper.attachShadow({ mode: "open" });
    shadow.appendChild(errorCard({ error: "nested row failed" }));
    host.appendChild(wrapper);
    expect(collectConfigErrors(host)).toEqual(["nested row failed"]);
  });

  it("collects multiple error cards in document order", () => {
    const host = document.createElement("div");
    host.appendChild(errorCard({ error: "first" }));
    const inner = document.createElement("div");
    inner.appendChild(errorCard({ error: "second" }));
    host.appendChild(inner);
    expect(collectConfigErrors(host)).toEqual(["first", "second"]);
  });
});

describe("DashboardPreview view controls", () => {
  it("uses ordinary pressed buttons instead of incomplete tab semantics", async () => {
    if (!customElements.get("hui-card")) {
      customElements.define("hui-card", class extends HTMLElement {});
    }
    render(React.createElement(DashboardPreview, { config: { views: [
      { title: "First", cards: [] },
      { title: "Second", cards: [] },
    ] } }));

    const group = await screen.findByRole("group", { name: "Dashboard views" });
    const first = screen.getByRole("button", { name: "First" });
    const second = screen.getByRole("button", { name: "Second" });
    expect(group).toContainElement(first);
    expect(first).toHaveAttribute("aria-pressed", "true");
    expect(second).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("tab")).toBeNull();

    fireEvent.click(second);
    expect(first).toHaveAttribute("aria-pressed", "false");
    expect(second).toHaveAttribute("aria-pressed", "true");
  });
});
