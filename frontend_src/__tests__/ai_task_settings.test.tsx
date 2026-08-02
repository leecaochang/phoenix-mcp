import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const listTokens = vi.fn();
const getAgentCliProviders = vi.fn();
const getAgentCliModels = vi.fn();
const getAiTaskPreferred = vi.fn();
const setAiTaskPreferred = vi.fn();
const clearAiTaskPreferred = vi.fn();

vi.mock("../api", () => ({
  api: {
    listTokens: (...a: unknown[]) => listTokens(...a),
    getAgentCliProviders: (...a: unknown[]) => getAgentCliProviders(...a),
    getAgentCliModels: (...a: unknown[]) => getAgentCliModels(...a),
    getAiTaskPreferred: (...a: unknown[]) => getAiTaskPreferred(...a),
    setAiTaskPreferred: (...a: unknown[]) => setAiTaskPreferred(...a),
    clearAiTaskPreferred: (...a: unknown[]) => clearAiTaskPreferred(...a),
  },
}));

function prefStatus(over: Record<string, unknown> = {}) {
  return {
    supported: true, entity_id: "ai_task.phoenix_mcp_ai_task",
    gen_data_entity_id: null, gen_data_name: null, is_preferred: false, ...over,
  };
}

import { AiTaskSettings } from "../components/AiTaskSettings";
import type { GlobalSettings } from "../types";

function settings(over: Partial<GlobalSettings> = {}): GlobalSettings {
  return {
    ai_task_enabled: false,
    ai_task_token_id: null,
    ai_task_provider_id: null,
    ai_task_model: null,
    ai_task_supported: true,
    ...(over as object),
  } as unknown as GlobalSettings;
}

function renderCard(s: GlobalSettings, onChange = vi.fn()) {
  render(<AiTaskSettings settings={s} onChange={onChange} saving={false} />);
  return { onChange };
}

describe("AiTaskSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTokens.mockResolvedValue([{ id: "tok-1", name: "ai-token" }]);
    getAgentCliProviders.mockResolvedValue({ instances: [{ id: "i1", kind: "claude", name: "Claude", model: "claude-opus-4-8" }] });
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8"] });
    getAiTaskPreferred.mockResolvedValue(prefStatus());
    setAiTaskPreferred.mockResolvedValue(prefStatus({ is_preferred: true, gen_data_entity_id: "ai_task.phoenix_mcp_ai_task" }));
    clearAiTaskPreferred.mockResolvedValue(prefStatus());
  });

  it("renders and toggles enable", async () => {
    const { onChange } = renderCard(settings());
    expect(screen.getByText("AI Task")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP AI Task"));
    expect(onChange).toHaveBeenCalledWith("ai_task_enabled", true);
    await waitFor(() => expect(listTokens).toHaveBeenCalled());
  });

  it("writes the token and provider selections", async () => {
    const { onChange } = renderCard(settings());
    await waitFor(() => expect(screen.getByRole("option", { name: "ai-token" })).toBeTruthy());
    fireEvent.change(screen.getByLabelText("AI Task token"), { target: { value: "tok-1" } });
    expect(onChange).toHaveBeenCalledWith("ai_task_token_id", "tok-1");
    fireEvent.change(screen.getByLabelText("AI Task provider account"), { target: { value: "i1" } });
    expect(onChange).toHaveBeenCalledWith("ai_task_provider_id", "i1");
  });

  it("model select is gated on a provider", async () => {
    const { rerender } = render(<AiTaskSettings settings={settings()} onChange={vi.fn()} saving={false} />);
    expect((screen.getByLabelText("AI Task model") as HTMLSelectElement).disabled).toBe(true);
    rerender(<AiTaskSettings settings={settings({ ai_task_provider_id: "i1" })} onChange={vi.fn()} saving={false} />);
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalledWith("i1"));
    expect((screen.getByLabelText("AI Task model") as HTMLSelectElement).disabled).toBe(false);
  });

  it("disables everything when the HA seam is unsupported", () => {
    renderCard(settings({ ai_task_supported: false }));
    expect((screen.getByLabelText("Enable Phoenix MCP AI Task") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("AI Task token") as HTMLSelectElement).disabled).toBe(true);
  });

  it("shows a Set-up-default button when the entity exists and is not yet the default", async () => {
    renderCard(settings());
    const btn = await screen.findByRole("button", { name: "Make default" });
    expect((btn as HTMLButtonElement).disabled).toBe(false);
  });

  it("confirms and sets Phoenix MCP as the default, warning about an existing default", async () => {
    getAiTaskPreferred.mockResolvedValue(
      prefStatus({ gen_data_entity_id: "ai_task.claude_ai_task", gen_data_name: "Claude AI Task" }),
    );
    renderCard(settings());
    fireEvent.click(await screen.findByRole("button", { name: "Make default" }));
    // Overwrite warning names the current default.
    expect(screen.getByText(/Claude AI Task/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Set as default" }));
    await waitFor(() => expect(setAiTaskPreferred).toHaveBeenCalled());
  });

  it("shows a Remove-default button when Phoenix MCP is the default, and clears it", async () => {
    getAiTaskPreferred.mockResolvedValue(
      prefStatus({ is_preferred: true, gen_data_entity_id: "ai_task.phoenix_mcp_ai_task" }),
    );
    renderCard(settings());
    fireEvent.click(await screen.findByRole("button", { name: "Remove default" }));
    await waitFor(() => expect(clearAiTaskPreferred).toHaveBeenCalled());
  });

  it("hides the AI Task setup section when the preferences seam is unavailable", async () => {
    getAiTaskPreferred.mockResolvedValue(prefStatus({ supported: false, entity_id: null }));
    renderCard(settings());
    await waitFor(() => expect(getAiTaskPreferred).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Make default" })).toBeNull();
  });

  it("pops the default-setup modal on first enable", async () => {
    renderCard(settings());
    await screen.findByRole("button", { name: "Make default" });  // pref loaded
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP AI Task"));
    expect(screen.getByText("Make Phoenix MCP the default AI Task?")).toBeTruthy();
  });

  it("confirms before disabling when Phoenix MCP is the current default", async () => {
    getAiTaskPreferred.mockResolvedValue(prefStatus({ is_preferred: true, gen_data_entity_id: "ai_task.phoenix_mcp_ai_task" }));
    const { onChange } = renderCard(settings({ ai_task_enabled: true }));
    await screen.findByRole("button", { name: "Remove default" });  // pref loaded + preferred
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP AI Task"));  // toggle off
    expect(onChange).not.toHaveBeenCalledWith("ai_task_enabled", false);
    expect(screen.getByText("Disable the Phoenix MCP AI Task?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disable and clear default" }));
    expect(onChange).toHaveBeenCalledWith("ai_task_enabled", false);
  });
});
