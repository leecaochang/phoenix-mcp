import { describe, it, expect } from "vitest";
import type { AgentCliProviderKind, MesaProfileScope, TokenRecord, VersionResourceType } from "../types";
import { CASCADING_SCOPES, EDITOR_SCOPES } from "../views/MesaView";
import { QUICK_ADD_SCOPES } from "../inject/QuickAdd";
import { SUBPAGE_SURFACES } from "../inject/dom";
import { RESOURCE_TYPE_LABEL_KEYS } from "../views/ChangesView";
import contract from "../../tests/contract/token_record_keys.json";
import versionContract from "../../tests/contract/version_resource_types.json";
import providerContract from "../../tests/contract/agentcli_provider_kinds.json";
import mesaContract from "../../tests/contract/mesa_scopes.json";

// Contract drift guard between the frontend TokenRecord type and the Python
// serializer. The shared fixture is generated from TokenRecord.to_dict (see
// tests/test_frontend_contract.py). This map is typed `satisfies
// Record<keyof TokenRecord, true>`, so tsc fails if a TokenRecord field is added
// or removed without updating it; the runtime assertion then checks those keys
// equal the Python-generated fixture, catching drift on either side.
const TOKEN_RECORD_KEYS = {
  id: true,
  name: true,
  created_at: true,
  created_by: true,
  expires_at: true,
  revoked: true,
  last_used_at: true,
  updated_at: true,
  pass_through: true,
  use_assist_exposure: true,
  announce_all_tools: true,
  confirm_inline_wait_seconds: true,
  persona: true,
  rate_limit_requests: true,
  rate_limit_burst: true,
  permissions: true,
  presets: true,
  active_preset_id: true,
  cap_config_read: true,
  cap_camera_read: true,
  cap_template_render: true,
  cap_log_read: true,
  cap_log_control: true,
  cap_search: true,
  cap_registry_read: true,
  cap_traces: true,
  cap_diagnostics: true,
  cap_broadcast: true,
  cap_service_response: true,
  cap_automation_write: true,
  cap_script_write: true,
  cap_blueprint_write: true,
  cap_scene_write: true,
  cap_helper_write: true,
  cap_physical_control: true,
  cap_restart: true,
  cap_integration_write: true,
  cap_integration_reconfigure: true,
  cap_lovelace_write: true,
  cap_registry_write: true,
  cap_radio_write: true,
  cap_energy_write: true,
  cap_backup: true,
  cap_filesystem: true,
  cap_yaml_edit: true,
  cap_esphome_yaml: true,
  cap_esphome_flash: true,
} satisfies Record<keyof TokenRecord, true>;

describe("frontend/backend TokenRecord contract", () => {
  it("the TS type's keys match the Python serializer fixture", () => {
    expect(Object.keys(TOKEN_RECORD_KEYS).sort()).toEqual(
      [...contract.token_record_keys].sort(),
    );
  });
});

// Same guard for version records. This union drifted twice without anyone
// noticing: blueprint, entity and esphome_yaml were recorded by the backend
// while missing here, so those versions were mistyped everywhere they were
// used. `satisfies Record<VersionResourceType, true>` makes tsc fail if the
// union changes without this map; the runtime check then pins both against the
// types the Python side actually records.
const VERSION_RESOURCE_TYPES = {
  automation: true,
  script: true,
  scene: true,
  helper: true,
  dashboard: true,
  blueprint: true,
  entity: true,
  device: true,
  yaml_config: true,
  esphome_yaml: true,
  energy: true,
  file: true,
  config_entry: true,
} satisfies Record<VersionResourceType, true>;

describe("frontend/backend version resource type contract", () => {
  it("the TS union covers exactly what _record_version records", () => {
    expect(Object.keys(VERSION_RESOURCE_TYPES).sort()).toEqual(
      [...versionContract.version_resource_types].sort(),
    );
  });

  // The union above is a TYPE. It says nothing about whether Changes can RENDER
  // the value, and resourceTypeLabel falls through to the raw slug rather than
  // failing, so a missing entry ships as a row labelled `energy` that an operator
  // scanning for "Energy dashboard" does not recognise. Live-hit exactly that way
  // on 2026-08-03: the rows were there and were reported as absent.
  it("every resource type has a label key, so no row renders as a raw slug", () => {
    const missing = Object.keys(VERSION_RESOURCE_TYPES).filter(
      (rt) => !(rt in RESOURCE_TYPE_LABEL_KEYS),
    );
    expect(missing).toEqual([]);
  });
});

// Contract drift guard for the Agent Chat provider allowlist, which is
// mirrored between the backend allowlist and the frontend's transport union.
// The dropdown itself is supplied by the backend registry at runtime and is
// pinned by the backend catalog tests.
const PROVIDER_KINDS = {
  claude: true,
  deepseek: true,
  chatgpt: true,
  gemini: true,
  grok: true,
  groq: true,
  kimi: true,
  meta: true,
  minimax: true,
  mistral: true,
  openrouter: true,
  nvidia: true,
  ollama: true,
  ollama_cloud: true,
  together: true,
  cerebras: true,
  fireworks: true,
  qwen: true,
  zai: true,
} satisfies Record<AgentCliProviderKind, true>;

describe("frontend/backend agentCLI provider contract", () => {
  it("the TS union covers exactly the backend allowlist", () => {
    expect(Object.keys(PROVIDER_KINDS).sort()).toEqual(
      [...providerContract.agentcli_provider_kinds].sort(),
    );
  });
});

// Contract drift guard for the MESA inheritance levels, which are hand-mirrored
// into the backend const, the editor's endpoint dispatch, six label maps, the
// list table and the in-context injector. Most of those are SILENT when one is
// missing: the dispatch used to end in an untyped fallthrough that wrote any
// unnamed scope to the area endpoint, and the injector coerced any unknown
// scope to entity. tsc covers the Records keyed by the union; this pins the
// union itself, the runtime tables derived from it, and the injector's subset.
const MESA_SCOPES = {
  entity: true,
  device: true,
  area: true,
  integration: true,
  domain: true,
} satisfies Record<MesaProfileScope, true>;

describe("frontend/backend MESA scope contract", () => {
  it("the TS union covers exactly the backend scope list", () => {
    expect(Object.keys(MESA_SCOPES).sort()).toEqual([...mesaContract.mesa_scopes].sort());
  });

  it("every scope has an endpoint trio in the editor's dispatch table", () => {
    expect([...EDITOR_SCOPES].sort()).toEqual([...mesaContract.mesa_scopes].sort());
  });

  it("the list table renders every cascading scope", () => {
    const cascading = mesaContract.mesa_scopes.filter((s) => s !== "entity");
    expect([...CASCADING_SCOPES].sort()).toEqual([...cascading].sort());
  });

  it("the injector opens the modal for exactly its declared scopes", () => {
    expect([...QUICK_ADD_SCOPES].sort()).toEqual([...mesaContract.mesa_injector_scopes].sort());
  });

  it("every injector scope is a real scope", () => {
    for (const scope of mesaContract.mesa_injector_scopes) {
      expect(mesaContract.mesa_scopes).toContain(scope);
    }
  });

  it("the injector's subpage surfaces cover every non-entity scope it declares", () => {
    // Entity scope rides the data tables instead, so it has no subpage surface.
    const expected = mesaContract.mesa_injector_scopes.filter((s) => s !== "entity");
    expect(SUBPAGE_SURFACES.map((s) => s.scope).sort()).toEqual([...expected].sort());
  });
});
