import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const listTokens = vi.fn();
const getAgentCliProviders = vi.fn();
const getAgentCliModels = vi.fn();
const createVoiceAgentPipeline = vi.fn();
const deleteVoiceAgentPipeline = vi.fn();

vi.mock("../api", () => ({
  api: {
    listTokens: (...a: unknown[]) => listTokens(...a),
    getAgentCliProviders: (...a: unknown[]) => getAgentCliProviders(...a),
    getAgentCliModels: (...a: unknown[]) => getAgentCliModels(...a),
    createVoiceAgentPipeline: (...a: unknown[]) => createVoiceAgentPipeline(...a),
    deleteVoiceAgentPipeline: (...a: unknown[]) => deleteVoiceAgentPipeline(...a),
  },
}));

import { VoiceAgentSettings } from "../components/VoiceAgentSettings";
import type { GlobalSettings } from "../types";

function settings(over: Partial<GlobalSettings> = {}): GlobalSettings {
  return {
    voice_agent_enabled: false,
    voice_agent_token_id: null,
    voice_agent_provider_id: null,
    voice_agent_model: null,
    voice_agent_pipeline_id: null,
    voice_agent_pipeline_supported: true,
    ...(over as object),
  } as unknown as GlobalSettings;
}

const FULL = {
  voice_agent_enabled: true,
  voice_agent_token_id: "tok-1",
  voice_agent_provider_id: "i1",
  voice_agent_model: "claude-opus-4-8",
};

function renderCard(s: GlobalSettings, onChange = vi.fn()) {
  render(<VoiceAgentSettings settings={s} onChange={onChange} saving={false} />);
  return { onChange };
}

describe("VoiceAgentSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTokens.mockResolvedValue([{ id: "tok-1", name: "voice-token" }]);
    getAgentCliProviders.mockResolvedValue({ instances: [{ id: "i1", kind: "claude", name: "Claude", model: "claude-opus-4-8" }] });
    getAgentCliModels.mockResolvedValue({ models: ["claude-opus-4-8", "claude-sonnet-5"] });
    createVoiceAgentPipeline.mockResolvedValue({ pipeline_id: "pl1", name: "Phoenix MCP", preferred: true });
    deleteVoiceAgentPipeline.mockResolvedValue({ ok: true });
  });

  it("renders the card and toggles enable", async () => {
    const { onChange } = renderCard(settings());
    expect(screen.getByText("Voice Agent")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP voice agent"));
    expect(onChange).toHaveBeenCalledWith("voice_agent_enabled", true);
    await waitFor(() => expect(listTokens).toHaveBeenCalled());
  });

  it("populates tokens and writes the token selection", async () => {
    const { onChange } = renderCard(settings());
    await waitFor(() => expect(screen.getByRole("option", { name: "voice-token" })).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Voice agent token"), { target: { value: "tok-1" } });
    expect(onChange).toHaveBeenCalledWith("voice_agent_token_id", "tok-1");
  });

  it("places select help above all three dropdowns without stacking the switch", () => {
    renderCard(settings());
    for (const label of ["Voice agent token", "Voice agent provider account", "Voice agent model"]) {
      expect(screen.getByLabelText(label).closest(".toggle-row")).toHaveClass("toggle-row-stacked-control");
    }
    expect(screen.getByLabelText("Enable Phoenix MCP voice agent").closest(".toggle-row"))
      .not.toHaveClass("toggle-row-stacked-control");
  });

  it("renders and saves voice conversation behavior controls", () => {
    const { onChange } = renderCard(settings());
    fireEvent.change(screen.getByLabelText("Voice Agent conversation style"), {
      target: { value: "lively" },
    });
    fireEvent.change(screen.getByLabelText("Voice Agent detail level"), {
      target: { value: "balanced" },
    });
    fireEvent.click(screen.getByLabelText("Voice Agent Home-focused mode"));
    expect(onChange).toHaveBeenCalledWith("voice_agent_conversation_style", "lively");
    expect(onChange).toHaveBeenCalledWith("voice_agent_detail_level", "balanced");
    expect(onChange).toHaveBeenCalledWith("voice_agent_home_focused", true);
  });

  it("model select is disabled until a provider is chosen, then loads models", async () => {
    const { rerender } = render(<VoiceAgentSettings settings={settings()} onChange={vi.fn()} saving={false} />);
    expect((screen.getByLabelText("Voice agent model") as HTMLSelectElement).disabled).toBe(true);

    rerender(<VoiceAgentSettings settings={settings({ voice_agent_provider_id: "i1" })} onChange={vi.fn()} saving={false} />);
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalledWith("i1"));
    await waitFor(() => expect(screen.getByRole("option", { name: "claude-sonnet-5" })).toBeTruthy());
    expect((screen.getByLabelText("Voice agent model") as HTMLSelectElement).disabled).toBe(false);
  });

  it("disables provider + model selects when no provider accounts exist", async () => {
    getAgentCliProviders.mockResolvedValue({ instances: [] });
    renderCard(settings());
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    await waitFor(() => expect((screen.getByLabelText("Voice agent provider account") as HTMLSelectElement).disabled).toBe(true));
    expect((screen.getByLabelText("Voice agent model") as HTMLSelectElement).disabled).toBe(true);
  });

  it("refetches provider accounts when the providers-changed event fires", async () => {
    renderCard(settings());
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new CustomEvent("phx-agentcli-providers-changed"));
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalledTimes(2));
  });

  it("opens the setup modal when enabling with no pipeline yet", async () => {
    renderCard(settings());
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP voice agent"));
    expect(screen.getByText("Set up the Phoenix MCP voice assistant")).toBeTruthy();
  });

  it("shows a setup button when fully configured and creates a preferred pipeline by default", async () => {
    renderCard(settings(FULL));
    const setupBtn = screen.getByRole("button", { name: "Set up Phoenix MCP assistant" });
    expect((setupBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(setupBtn);
    // Modal open; preferred defaults on; "Set up automatically" enabled.
    const autoBtn = screen.getByRole("button", { name: "Set up automatically" });
    expect((autoBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(autoBtn);
    await waitFor(() => expect(createVoiceAgentPipeline).toHaveBeenCalledWith(true));
  });

  it("disables the setup button until fully configured", () => {
    renderCard(settings({ voice_agent_enabled: true }));  // enabled but no token/provider/model
    expect((screen.getByRole("button", { name: "Set up Phoenix MCP assistant" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows a remove button and deletes the pipeline after confirming", async () => {
    renderCard(settings({ ...FULL, voice_agent_pipeline_id: "pl1" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Phoenix MCP assistant" }));
    // Confirm modal appears; deletion happens only after confirming.
    expect(deleteVoiceAgentPipeline).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove assistant" }));
    await waitFor(() => expect(deleteVoiceAgentPipeline).toHaveBeenCalled());
  });

  it("hides the one-click setup when the HA seam is unsupported", () => {
    renderCard(settings({ ...FULL, voice_agent_pipeline_supported: false }));
    expect(screen.queryByRole("button", { name: "Set up Phoenix MCP assistant" })).toBeNull();
  });

  it("confirms before disabling when a pipeline exists", () => {
    const { onChange } = renderCard(settings({ ...FULL, voice_agent_pipeline_id: "pl1" }));
    fireEvent.click(screen.getByLabelText("Enable Phoenix MCP voice agent"));  // toggle off
    expect(onChange).not.toHaveBeenCalledWith("voice_agent_enabled", false);
    expect(screen.getByText("Disable the Phoenix MCP voice agent?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disable and remove assistant" }));
    expect(onChange).toHaveBeenCalledWith("voice_agent_enabled", false);
  });
});
