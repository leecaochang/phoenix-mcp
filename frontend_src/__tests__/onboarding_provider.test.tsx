import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const getAgentCliProviders = vi.fn();
const probeAgentCliProvider = vi.fn();

vi.mock("../api", () => ({
  localizedApiMessage: (message: string) => message,
  api: {
    getAgentCliProviders: (...args: unknown[]) => getAgentCliProviders(...args),
    probeAgentCliProvider: (...args: unknown[]) => probeAgentCliProvider(...args),
    createAgentCliProvider: vi.fn(),
    probeAgentCliCapabilities: vi.fn(),
  },
}));

import { WizardProviderSetup } from "../views/OnboardingWizard";

const providerTypes = [
  {
    kind: "zai", label: "Z.ai", label_key: "settings.providerZai", fields: [
      {
        id: "endpoint_id", type: "choice", required: true,
        label_key: "settings.agentcliZaiPlan", choices: [
          { value: "standard", label: "Standard API", label_key: "settings.agentcliZaiStandard" },
          { value: "coding", label: "Coding Plan", label_key: "settings.agentcliZaiCoding" },
        ],
      },
      { id: "api_key", type: "secret", required: true, label_key: "settings.agentcliApiKey" },
    ],
  },
];

function renderProviderStep() {
  return render(<WizardProviderSetup onBack={() => {}} onTryNow={() => {}} />);
}

describe("onboarding provider picker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAgentCliProviders.mockResolvedValue({ instances: [], provider_types: providerTypes });
  });

  it("starts with the dropdown placeholder and opens the catalog fields", async () => {
    renderProviderStep();
    const picker = await screen.findByLabelText("Provider type") as HTMLSelectElement;
    expect(picker.value).toBe("");
    expect(picker.options[0].textContent).toBe("Add new provider…");
    expect(screen.queryByRole("button", { name: "Add new provider…" })).toBeNull();

    fireEvent.change(picker, { target: { value: "zai" } });
    expect(await screen.findByLabelText("Z.ai plan")).toBeTruthy();
    expect(screen.getByLabelText("API key")).toBeTruthy();
    expect(picker.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(picker.disabled).toBe(false));
    expect(picker.value).toBe("");
    await waitFor(() => expect(document.activeElement).toBe(picker));
  });

  it("resets the picker on failed validation while retaining the form", async () => {
    probeAgentCliProvider.mockResolvedValue({ ok: false, error: "Bad key", models: [] });
    renderProviderStep();
    const picker = await screen.findByLabelText("Provider type") as HTMLSelectElement;
    fireEvent.change(picker, { target: { value: "zai" } });
    const key = await screen.findByLabelText("API key") as HTMLInputElement;
    fireEvent.change(key, { target: { value: "secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await screen.findByText("Bad key");
    expect(picker.value).toBe("");
    expect(key.value).toBe("secret-value");
    expect(screen.getByLabelText("Z.ai plan")).toBeTruthy();
  });
});
