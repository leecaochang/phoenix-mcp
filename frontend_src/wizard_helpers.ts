// Pure helpers for the onboarding wizard, kept here so they can be unit-tested
// without rendering React components.
import type { EntityTree, PermissionTree } from "./types";
import { t } from "./i18n";

// The Phoenix MCP MCP endpoint, built from the origin the admin is currently browsing.
export function buildMcpUrl(origin: string): string {
  return `${origin.replace(/\/+$/, "")}/api/phoenix-mcp`;
}

// The unauthenticated Agent Skill guide endpoint (Channel B).
export const SKILL_PATH = "/api/phoenix-mcp/skill";

// Derive the skill guide URL from the (possibly admin-edited) MCP URL so both
// point at the same host the agent will reach. Falls back to appending the path
// if the MCP URL was changed to something that does not end in the MCP path.
export function skillUrlFromMcp(mcpUrl: string): string {
  return mcpUrl.replace(/\/api\/phoenix-mcp\/?$/, "") + SKILL_PATH;
}

// The per-token MCP server name used in agent configs. Derived from the token
// name so each token gets a DISTINCT client entry: configure one entry per
// token once, then switch tokens by toggling which entry is enabled in the
// client. Token names match ^[A-Za-z0-9_-]{3,32}$ (backend TOKEN_NAME_REGEX)
// and the backend rejects two tokens whose lowercased name slugs collide, so
// this derivation is unique across tokens without extra sanitization.
export function mcpServerName(tokenName: string): string {
  return "phx-" + tokenName.toLowerCase().replace(/_/g, "-");
}

// The env var Codex's config.toml reads the token from. Must also be unique
// per token: two [mcp_servers.*] tables sharing one env var would collide.
// The PHOENIX_TOKEN_ prefix keeps it a valid identifier for any token name.
export function codexTokenEnv(tokenName: string): string {
  return "PHOENIX_TOKEN_" + tokenName.toUpperCase().replace(/-/g, "_");
}

// The verified `claude mcp add` command. Phoenix MCP authenticates via
// `Authorization: Bearer phx_<token>` (NOT X-API-Key, NOT OAuth) and the modern
// Streamable HTTP transport is `--transport http`.
export function buildClaudeCommand(url: string, token: string, serverName: string): string {
  return `claude mcp add --transport http ${serverName} ${url} --header "Authorization: Bearer ${token}"`;
}

// A generic MCP server config block for clients that use a JSON file
// (the MCP spec calls this transport streamable-http; "http" is the common alias).
export function buildMcpJson(url: string, token: string, serverName: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        [serverName]: {
          type: "http",
          url,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

// Gemini CLI: `gemini mcp add <name> <url> --transport http --header ...`.
export function buildGeminiCommand(url: string, token: string, serverName: string): string {
  return `gemini mcp add ${serverName} ${url} --transport http --header "Authorization: Bearer ${token}"`;
}

// The value for an Authorization header (for GUI clients that take a header
// key/value, like Codex's "Connect to a custom MCP" form).
export function buildBearerValue(token: string): string {
  return `Bearer ${token}`;
}

// Codex's `codex mcp add` CLI is stdio-only; remote HTTP servers are added via
// its GUI or ~/.codex/config.toml, which reads the token from an env var.
export function buildCodexEnv(token: string, envVar: string): string {
  return `export ${envVar}="${token}"`;
}
export function buildCodexConfig(url: string, serverName: string, envVar: string): string {
  return `[mcp_servers.${serverName}]\nurl = "${url}"\nbearer_token_env_var = "${envVar}"`;
}

// Cursor reads remote servers from ~/.cursor/mcp.json using url + headers
// (no "type" field), so it gets its own builder.
export function buildCursorJson(url: string, token: string, serverName: string): string {
  return JSON.stringify(
    { mcpServers: { [serverName]: { url, headers: { Authorization: `Bearer ${token}` } } } },
    null,
    2,
  );
}

export interface AgentBlock {
  title?: string;
  hint?: string;
  code?: string;
  // Labeled key/value pairs to copy individually (e.g. a GUI header key + value).
  fields?: { label: string; value: string }[];
}
export interface AgentTab {
  key: string;
  label: string;
  href: string;
  intro?: string;
  blocks: AgentBlock[];
  showSkillInstall?: boolean;
}

// Per-agent connection instructions, default first (Claude Code). Verified
// against each tool's current docs; DeepSeek is a model used via an MCP client,
// so it gets the generic config rather than a CLI command. tokenName drives
// the per-token server name (and Codex env var), so every token can coexist
// as its own entry in one client.
export function buildAgentTabs(url: string, token: string, tokenName: string): AgentTab[] {
  const serverName = mcpServerName(tokenName);
  const envVar = codexTokenEnv(tokenName);
  return [
    {
      key: "claude",
      label: "Claude Code",
      href: "https://docs.claude.com/en/docs/claude-code/mcp",
      blocks: [
        { hint: t("wizard.agentClaudeHint"), code: buildClaudeCommand(url, token, serverName) },
      ],
    },
    {
      key: "claude-web",
      label: t("wizard.agentClaudeWebLabel"),
      href: "https://claude.com/docs/connectors/custom/remote-mcp",
      intro: t("wizard.agentClaudeWebIntro"),
      blocks: [
        {
          title: t("wizard.agentClaudeWebSetupTitle"),
          hint: t("wizard.agentClaudeWebSetupHint"),
          fields: [
            { label: t("wizard.codexHeaderKeyLabel"), value: "Authorization" },
            { label: t("wizard.codexHeaderValueLabel"), value: buildBearerValue(token) },
          ],
        },
      ],
      showSkillInstall: false,
    },
    {
      key: "gemini",
      label: "Gemini CLI",
      href: "https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html",
      blocks: [{ hint: t("wizard.agentGeminiHint"), code: buildGeminiCommand(url, token, serverName) }],
    },
    {
      key: "codex",
      label: "Codex",
      href: "https://developers.openai.com/codex/mcp",
      intro: t("wizard.agentCodexIntro"),
      blocks: [
        {
          title: t("wizard.codexAppSettingsTitle"),
          hint: t("wizard.codexAppSettingsHint", { serverName }),
          fields: [
            { label: t("wizard.codexServerNameLabel"), value: serverName },
            { label: t("wizard.codexHeaderKeyLabel"), value: "Authorization" },
            { label: t("wizard.codexHeaderValueLabel"), value: buildBearerValue(token) },
          ],
        },
        {
          title: t("wizard.codexConfigFileTitle"),
          hint: t("wizard.codexConfigFileHint"),
          code: buildCodexConfig(url, serverName, envVar),
        },
        {
          title: t("wizard.codexEnvTitle", { envVar }),
          hint: t("wizard.codexEnvHint", { envVar }),
          code: buildCodexEnv(token, envVar),
        },
      ],
    },
    {
      key: "cursor",
      label: "Cursor",
      href: "https://cursor.com/docs/mcp",
      intro: t("wizard.agentCursorIntro"),
      blocks: [{ code: buildCursorJson(url, token, serverName) }],
    },
    {
      key: "other",
      label: t("wizard.agentOtherLabel"),
      href: "https://modelcontextprotocol.io/",
      intro: t("wizard.agentOtherIntro"),
      blocks: [{ code: buildMcpJson(url, token, serverName) }],
    },
  ];
}

// Per-agent "install the Phoenix MCP skill" guidance (Channel B). Channel A already
// links every connection to the same guide, so installing it locally is
// optional, but it makes the agent use Phoenix MCP correctly from the first turn. Only
// Claude Code has a first-class skills directory; other agents load a project
// context or rules file, so for those we download the guide and name where to
// reference it. agentKey matches the keys from buildAgentTabs.
export function buildSkillInstall(skillUrl: string, agentKey: string): AgentBlock[] {
  const title = t("wizard.skillInstallTitle");
  switch (agentKey) {
    case "claude":
      return [{
        title,
        hint: t("wizard.skillClaudeHint"),
        code: `mkdir -p ~/.claude/skills/phoenix && curl -fsSL ${skillUrl} -o ~/.claude/skills/phoenix/SKILL.md`,
      }];
    case "cursor":
      return [{
        title,
        hint: t("wizard.skillCursorHint"),
        code: `mkdir -p .cursor/rules && curl -fsSL ${skillUrl} -o .cursor/rules/phoenix.md`,
      }];
    case "gemini":
      return [{
        title,
        hint: t("wizard.skillGeminiHint"),
        fields: [{ label: t("wizard.skillGuideUrlLabel"), value: skillUrl }],
        code: `curl -fsSL ${skillUrl} -o phoenix.md`,
      }];
    case "codex":
      return [{
        title,
        hint: t("wizard.skillCodexHint"),
        fields: [{ label: t("wizard.skillGuideUrlLabel"), value: skillUrl }],
        code: `curl -fsSL ${skillUrl} -o phoenix.md`,
      }];
    default:
      return [{
        title,
        hint: t("wizard.skillOtherHint"),
        fields: [{ label: t("wizard.skillGuideUrlLabel"), value: skillUrl }],
        code: `curl -fsSL ${skillUrl} -o phoenix.md`,
      }];
  }
}

export interface TestPrompts {
  // A benign read that works in any MESA mode and reliably trips connection
  // detection (any authenticated MCP call counts).
  read: string;
  // A control action, suppressed under MESA enforced mode where it may require
  // admin confirmation and would not cleanly demonstrate the connection.
  action: string | null;
}

export function buildTestPrompt(friendlyName: string, mesaEnforced: boolean): TestPrompts {
  // Translated on purpose: the prompt is what the agent reads, so its language
  // is also the instruction for which language to answer in. An English prompt
  // in a Chinese panel gets an English reply.
  return {
    read: t("wizard.promptRead"),
    action: mesaEnforced ? null : t("wizard.promptAction", { name: friendlyName }),
  };
}

// The first entity granted full (GREEN = WRITE) access in a permission tree, or
// null. The wizard grants exactly one, so this identifies the chosen entity.
export function firstGreenEntity(tree: PermissionTree): string | null {
  for (const [entityId, node] of Object.entries(tree.entities)) {
    if (node.state === "GREEN") return entityId;
  }
  return null;
}

// Resolve the granted target to a concrete entity ID, accepting a grant made at
// the entity, device, or domain level (a device or domain grant cascades to its
// entities). Returns the first matching entity so the wizard always has a real
// entity to build its test prompt from, no matter which node the user clicked.
export function firstGreenTarget(tree: PermissionTree, entityTree: EntityTree | null): string | null {
  const direct = firstGreenEntity(tree);
  if (direct) return direct;
  if (!entityTree) return null;

  const greenDevices = new Set(
    Object.entries(tree.devices).filter(([, n]) => n.state === "GREEN").map(([d]) => d),
  );
  const greenDomains = new Set(
    Object.entries(tree.domains).filter(([, n]) => n.state === "GREEN").map(([d]) => d),
  );
  if (greenDevices.size === 0 && greenDomains.size === 0) return null;

  for (const [domain, dt] of Object.entries(entityTree)) {
    if (greenDomains.has(domain)) {
      const ids = Object.keys(dt.entity_details);
      if (ids.length) return ids[0];
    }
    for (const [eid, info] of Object.entries(dt.entity_details)) {
      if (info.device_id && greenDevices.has(info.device_id)) return eid;
    }
  }
  return null;
}
