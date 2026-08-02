import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const listTokens = vi.fn();

vi.mock("../api", () => ({
  api: { listTokens: (...a: unknown[]) => listTokens(...a) },
}));

import { AssistBridgeSettings } from "../components/AssistBridgeSettings";
import type { GlobalSettings } from "../types";

function settings(over: Partial<GlobalSettings> = {}): GlobalSettings {
  return { assist_api_supported: true, assist_bound_token_id: null, ...(over as object) } as unknown as GlobalSettings;
}

describe("AssistBridgeSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTokens.mockResolvedValue([{ id: "tok-1", name: "voice-token" }]);
  });

  it("lists tokens and binds the selected one", async () => {
    const onChange = vi.fn();
    render(<AssistBridgeSettings settings={settings()} onChange={onChange} saving={false} />);
    await waitFor(() => expect(screen.getByRole("option", { name: "voice-token" })).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Assist bound token"), { target: { value: "tok-1" } });
    expect(onChange).toHaveBeenCalledWith("assist_bound_token_id", "tok-1");
  });

  it("unbinds via the 'Not bound' option (empty string)", async () => {
    const onChange = vi.fn();
    render(<AssistBridgeSettings settings={settings({ assist_bound_token_id: "tok-1" })} onChange={onChange} saving={false} />);
    await waitFor(() => expect(listTokens).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Assist bound token"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith("assist_bound_token_id", "");
  });

  it("reflects the currently bound token", async () => {
    render(<AssistBridgeSettings settings={settings({ assist_bound_token_id: "tok-1" })} onChange={vi.fn()} saving={false} />);
    await waitFor(() => expect((screen.getByLabelText("Assist bound token") as HTMLSelectElement).value).toBe("tok-1"));
  });

  it("disables the dropdown when unsupported", () => {
    render(<AssistBridgeSettings settings={settings({ assist_api_supported: false })} onChange={vi.fn()} saving={false} />);
    expect((screen.getByLabelText("Assist bound token") as HTMLSelectElement).disabled).toBe(true);
  });
});
