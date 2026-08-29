/** Focused accessibility regressions for custom Phoenix MCP panel controls. */
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PermissionSelector } from "../components/PermissionSelector";
import { TagInput } from "../components/TagInput";
import { AuditTable } from "../components/AuditTable";
import { TokenCreateModal } from "../components/TokenCreateModal";
import { Loading } from "../components/common";
import type { AuditEntry } from "../types";

describe("accessibility regressions", () => {
  it("names permission selectors and each permission button", () => {
    render(
      <PermissionSelector
        value="GREEN"
        onChange={() => undefined}
        label="Permission for light.kitchen"
      />,
    );

    expect(screen.getByRole("group", { name: "Permission for light.kitchen" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Read and write, selected/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("connects the tag combobox to its active suggestion", () => {
    render(
      <TagInput
        value={[]}
        onChange={() => undefined}
        canonicalTags={["lighting.ambient", "lighting.task"]}
      />,
    );

    const combo = screen.getByRole("combobox", { name: "Semantic tags" });
    fireEvent.change(combo, { target: { value: "light" } });
    fireEvent.keyDown(combo, { key: "ArrowDown" });

    expect(combo).toHaveAttribute("aria-controls");
    expect(combo).toHaveAttribute("aria-activedescendant");
    expect(screen.getByRole("listbox")).toHaveAttribute("id", combo.getAttribute("aria-controls"));
  });

  it("opens audit row details from a named button", () => {
    const entry: AuditEntry = {
      request_id: "req-1",
      timestamp: "2026-06-30T00:00:00Z",
      token_id: "tok-1",
      token_name: "demo",
      method: "GET",
      resource: "/api/phoenix-mcp/states",
      outcome: "allowed",
      client_ip: "127.0.0.1",
      pass_through: false,
      payload: null,
    };

    render(
      <AuditTable
        entries={[entry]}
        tokenNames={{ "tok-1": "demo" }}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: /Open audit entry Allowed for demo/ })[0]);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Audit Entry")).toBeInTheDocument();
  });

  it("uses arrows to scan audit entries in their visible sort order", () => {
    const older: AuditEntry = {
      request_id: "req-older",
      timestamp: "2026-06-29T00:00:00Z",
      token_id: "tok-1",
      token_name: "demo",
      method: "GET",
      resource: "/api/older",
      outcome: "allowed",
      client_ip: "127.0.0.1",
      pass_through: false,
      payload: null,
    };
    const newer: AuditEntry = { ...older, request_id: "req-newer", timestamp: "2026-06-30T00:00:00Z", resource: "/api/newer" };
    render(<AuditTable entries={[older, newer]} />);
    fireEvent.click(screen.getAllByRole("button", { name: /Open audit entry Allowed for demo/ })[0]);
    expect(screen.getByRole("dialog")).toHaveTextContent("/api/newer");

    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getByRole("dialog")).toHaveTextContent("/api/older");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("/api/newer");
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(screen.getByRole("dialog")).toHaveTextContent("/api/newer");
  });

  it("keeps approval lifecycle rows distinct when they share a request id", () => {
    const approved: AuditEntry = {
      request_id: "approve-request",
      timestamp: "2026-08-29T07:00:00Z",
      token_id: "admin",
      token_name: "admin:user",
      method: "approval/approved",
      resource: "approval:restart_ha:appr-1",
      outcome: "allowed",
      client_ip: "",
      pass_through: false,
      payload: null,
    };
    const executed: AuditEntry = {
      ...approved,
      timestamp: "2026-08-29T07:00:01Z",
      method: "approval/executed",
    };
    render(<AuditTable entries={[approved, executed]} />);

    fireEvent.click(screen.getAllByRole("button", {
      name: /Open audit entry Allowed for admin/,
    })[0]);
    expect(screen.getByRole("dialog")).toHaveTextContent("approval/executed");
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getByRole("dialog")).toHaveTextContent("approval/approved");
  });

  it("wires the whole row as a single accessible click target, not one per cell", () => {
    // The row's mouse-clickable-anywhere behavior comes from a CSS overlay
    // (the button's ::after stretched via position:relative on the <tr>,
    // see .row-open in phoenix-mcp-panel.css) that jsdom cannot paint or hit-test, so
    // this checks what unit tests actually can: there is exactly one
    // focusable control per row (no per-cell tab stops, the accessibility
    // anti-pattern this must avoid), and the CSS hooks it depends on are wired.
    const entry: AuditEntry = {
      request_id: "req-2",
      timestamp: "2026-06-30T00:00:00Z",
      token_id: "tok-1",
      token_name: "demo",
      method: "GET",
      resource: "/api/phoenix-mcp/states",
      outcome: "allowed",
      client_ip: "127.0.0.1",
      pass_through: false,
      payload: null,
    };

    const { container } = render(
      <AuditTable
        entries={[entry]}
        tokenNames={{ "tok-1": "demo" }}
      />,
    );

    expect(screen.getAllByRole("button", { name: /Open audit entry/ })).toHaveLength(1);
    expect(container.querySelector("tr.clickable button.row-open")).not.toBeNull();
  });

  it("associates the token-name validation error with the input", () => {
    render(
      <TokenCreateModal
        existingNames={[]}
        onCreated={() => undefined}
        onClose={() => undefined}
        onOpenSettings={() => undefined}
      />,
    );

    const input = screen.getByLabelText("Name (required)");
    // Valid name: no error state advertised.
    fireEvent.change(input, { target: { value: "my_token" } });
    expect(input).not.toHaveAttribute("aria-invalid");
    // Too-short name: aria-invalid + described-by the alert-role error.
    fireEvent.change(input, { target: { value: "ab" } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-describedby", "token-name-error");
    const error = screen.getByRole("alert");
    expect(error).toHaveAttribute("id", "token-name-error");
    expect(error.textContent).toMatch(/3-32 characters/);
  });

  it("exposes the shared loading indicator as a status with a decorative spinner", () => {
    const { container } = render(<Loading />);
    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Loading...");
    expect(container.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");
  });
});
