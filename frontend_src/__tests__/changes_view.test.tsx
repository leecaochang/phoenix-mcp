import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ChangesView } from "../views/ChangesView";
import { api } from "../api";
import type { VersionSummary } from "../types";

vi.mock("../api", () => ({
  api: {
    listVersions: vi.fn(),
    getVersion: vi.fn(),
    restoreVersion: vi.fn(),
    listTokens: vi.fn().mockResolvedValue([]),
    listArchivedTokens: vi.fn().mockResolvedValue([]),
  },
}));

function version(over: Partial<VersionSummary> = {}): VersionSummary {
  return {
    id: "v1",
    resource_type: "automation",
    resource_id: "aid1",
    alias: "My automation",
    action: "create",
    token_id: "t1",
    token_name: "my_token",
    approved_by_user_id: null,
    timestamp: "2026-01-01T00:00:00Z",
    has_before: false,
    has_after: true,
    ...over,
  };
}

describe("ChangesView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("loads the first page and shows Load more only while more rows remain", async () => {
    vi.mocked(api.listVersions).mockResolvedValue({
      resource_type: null, resource_id: null,
      versions: [version({ id: "v1", alias: "First change" })],
      total: 2,
    });

    const { findByText, queryByText } = render(<ChangesView hass={null} />);
    await findByText("First change");

    expect(api.listVersions).toHaveBeenCalledWith({ limit: 100, offset: 0 });
    expect(queryByText("Load more")).toBeTruthy();
  });

  it("Load more appends the next page at the accumulated offset", async () => {
    vi.mocked(api.listVersions)
      .mockResolvedValueOnce({
        resource_type: null, resource_id: null,
        versions: [version({ id: "v1", alias: "First change" })], total: 2,
      })
      .mockResolvedValueOnce({
        resource_type: null, resource_id: null,
        versions: [version({ id: "v2", alias: "Second change" })], total: 2,
      });

    const { findByText, getByText, queryByText } = render(<ChangesView hass={null} />);
    await findByText("First change");

    fireEvent.click(getByText("Load more"));
    await findByText("Second change");

    expect(api.listVersions).toHaveBeenLastCalledWith({ limit: 100, offset: 1 });
    expect(queryByText("Load more")).toBeNull();
  });

  it("a manual refresh reloads from offset 0, replacing the loaded feed", async () => {
    vi.mocked(api.listVersions).mockResolvedValue({
      resource_type: null, resource_id: null,
      versions: [version({ id: "v1", alias: "First change" })], total: 1,
    });
    const { findByText, getByLabelText } = render(<ChangesView hass={null} />);
    await findByText("First change");

    fireEvent.click(getByLabelText("Refresh changes"));

    await waitFor(() =>
      expect(api.listVersions).toHaveBeenLastCalledWith({ limit: 100, offset: 0 }),
    );
  });
});

// A device YAML version is a raw text snapshot ({content}), the same shape as
// yaml_config and file. Rendering it through the structured path instead dumped
// {content, path} back as YAML, burying the file inside a "content: |-" block
// with every line re-indented.
describe("ChangesView device YAML detail", () => {
  const DEVICE = 'esphome:\n  name: rf-blaster2\nlogger:\n  level: WARN\n';

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // jsdom does not implement matchMedia, and the detail view reads it to pick
    // its default layout. Stubbed locally rather than globally: nothing else in
    // the suite renders this component.
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false, media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    }));
  });

  async function openDetail(after = DEVICE) {
    const summary = version({
      id: "v9", resource_type: "esphome_yaml", resource_id: "rf-blaster-2.yaml",
      alias: "rf-blaster-2.yaml", action: "edit", has_before: true, has_after: true,
    });
    vi.mocked(api.listVersions).mockResolvedValue({
      resource_type: null, resource_id: null, versions: [summary], total: 1,
    });
    vi.mocked(api.getVersion).mockResolvedValue({
      ...summary,
      before: { content: DEVICE },
      after: { content: after },
    } as never);

    const view = render(<ChangesView hass={null} />);
    const row = await view.findByText("rf-blaster-2.yaml");
    fireEvent.click(row);
    await waitFor(() => expect(api.getVersion).toHaveBeenCalledWith("v9"));
    await waitFor(() => expect(view.container.querySelector(".yaml-diff-cols")).toBeTruthy());
    return view;
  }

  it("renders the file itself, not a config: block wrapping it", async () => {
    const { container } = await openDetail();
    const text = container.textContent ?? "";
    expect(text).toContain("name: rf-blaster2");
    // The structured path would have produced this wrapper.
    expect(text).not.toContain("content: |");
  });

  it("offers the code view and switches the panes out of the line diff", async () => {
    const view = await openDetail(DEVICE.replace("WARN", "DEBUG"));
    expect(view.container.querySelectorAll(".diff-line").length).toBeGreaterThan(0);

    fireEvent.click(view.getByRole("button", { name: /code editor view/i }));

    expect(view.container.querySelectorAll(".diff-line").length).toBe(0);
    expect(view.container.textContent).toContain("name: rf-blaster2");
    expect(view.container.querySelector(".change-diff-hint")?.textContent)
      .toContain("not marked");
  });

  it("shares the choice with the approval diff rather than keeping its own", async () => {
    // storedCodeView is one key; choosing it on either surface applies on both.
    localStorage.setItem("phx-diff-code-view", "code");
    const { container } = await openDetail();
    expect(container.querySelectorAll(".diff-line").length).toBe(0);
  });
});
