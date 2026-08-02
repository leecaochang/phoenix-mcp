import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { ExportModal, ImportModal } from "../views/MesaView";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    exportMesaProfiles: vi.fn(),
    importMesaProfiles: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

const archive = {
  mesa_export: {
    format_version: "1.0",
    entities: { "light.a": {}, "lock.b": {} },
    domains: { lock: {} },
    integrations: {},
    areas: {},
  },
};

function pickFile(container: HTMLElement, contents: string) {
  const input = container.querySelector<HTMLInputElement>("input[type=file]")!;
  const file = new File([contents], "profiles.json", { type: "application/json" });
  // jsdom's File lacks text() in some versions; normalize it.
  Object.defineProperty(file, "text", { value: () => Promise.resolve(contents) });
  fireEvent.change(input, { target: { files: [file] } });
}

describe("ExportModal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("downloads the archive only after the explicit confirm", async () => {
    vi.mocked(api.exportMesaProfiles).mockResolvedValue(archive);
    const clicks: string[] = [];
    globalThis.URL.createObjectURL = vi.fn(() => "blob:x");
    globalThis.URL.revokeObjectURL = vi.fn();
    const origClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { clicks.push(this.getAttribute("download") ?? ""); };

    const onClose = vi.fn();
    const { getByText } = render(<ExportModal profileCount={3} onClose={onClose} />);
    expect(getByText(/Download 3 MESA profiles/i)).toBeInTheDocument();
    expect(api.exportMesaProfiles).not.toHaveBeenCalled();

    fireEvent.click(getByText("Export"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(api.exportMesaProfiles).toHaveBeenCalledOnce();
    expect(clicks).toHaveLength(1);
    expect(clicks[0]).toMatch(/^phoenix-mcp-mesa-profiles-\d{4}-\d{2}-\d{2}\.json$/);

    HTMLAnchorElement.prototype.click = origClick;
  });
});

describe("ImportModal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps Import disabled until a valid archive is picked, then summarizes it", async () => {
    const { container, getByText, findByText } = render(<ImportModal onClose={() => {}} />);
    const importBtn = getByText("Import") as HTMLButtonElement;
    expect(importBtn.disabled).toBe(true);

    pickFile(container, JSON.stringify(archive));
    await findByText(/Archive contains/);
    expect(getByText(/2 entity, 1 domain/)).toBeInTheDocument();
    expect((getByText("Import") as HTMLButtonElement).disabled).toBe(false);
  });

  it("rejects a file without the mesa_export root", async () => {
    const { container, findByText, getByText } = render(<ImportModal onClose={() => {}} />);
    pickFile(container, JSON.stringify({ nope: 1 }));
    await findByText(/missing the mesa_export root/i);
    expect((getByText("Import") as HTMLButtonElement).disabled).toBe(true);
  });

  it("rejects a non-JSON file", async () => {
    const { container, findByText } = render(<ImportModal onClose={() => {}} />);
    pickFile(container, "not json {");
    await findByText(/Could not read the file as JSON/i);
  });

  it("defaults to skip; the replace checkbox turns the confirm destructive", async () => {
    vi.mocked(api.importMesaProfiles).mockResolvedValue({
      imported: 2, overwritten: 0, skipped_existing: [], invalid: {},
    });
    const { container, getByText, findByText, getByLabelText } = render(<ImportModal onClose={() => {}} />);
    pickFile(container, JSON.stringify(archive));
    await findByText(/Archive contains/);
    expect(getByText(/left unchanged/)).toBeInTheDocument();

    fireEvent.click(getByLabelText(/Replace existing profiles/i));
    expect(getByText(/Profiles with matching keys will be replaced/)).toBeInTheDocument();
    const confirm = getByText("Import and replace") as HTMLButtonElement;
    expect(confirm.className).toContain("btn-danger");

    fireEvent.click(confirm);
    await findByText("Import complete");
    expect(api.importMesaProfiles).toHaveBeenCalledWith(archive, "overwrite");
  });

  it("imports with skip and reports the result; Close refreshes", async () => {
    vi.mocked(api.importMesaProfiles).mockResolvedValue({
      imported: 1, overwritten: 0, skipped_existing: ["entities:light.a"],
      invalid: { "entities:lock.b": "bad control_mode" },
    });
    const onClose = vi.fn();
    const { container, getByText, findByText } = render(<ImportModal onClose={onClose} />);
    pickFile(container, JSON.stringify(archive));
    await findByText(/Archive contains/);

    fireEvent.click(getByText("Import"));
    await findByText("Import complete");
    expect(api.importMesaProfiles).toHaveBeenCalledWith(archive, "skip");
    expect(getByText(/1 profile added, 0 replaced, 1 skipped/)).toBeInTheDocument();
    expect(getByText(/failed validation/)).toBeInTheDocument();
    expect(getByText("entities:lock.b")).toBeInTheDocument();

    fireEvent.click(getByText("Close"));
    expect(onClose).toHaveBeenCalledWith(true);
  });
});
