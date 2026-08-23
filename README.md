# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


**English** | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md)

Phoenix MCP gives your AI agents scoped, least-privilege access to Home Assistant. Each client gets its own token, limited to exactly the entities you allow, with its own capabilities, rate limit, and optional expiry. Auditing is enabled by default and configurable per outcome, any token can be revoked instantly, and the per-entity semantic safety layer (MESA) can make a device confirm-only or off-limits by its nature, no matter what permissions a token is granted.

Phoenix MCP runs entirely inside Home Assistant, with no extra process, no cloud dependency in the core server, and no setup beyond the Phoenix MCP panel (the optional Agent Chat, voice, and AI Task features send conversations to the model provider you configure, unless you point them at a local Ollama). It works with the MCP clients you already use (Claude Code, Cursor, ChatGPT/Codex, Gemini, and others), or you can skip installing a client altogether and chat from inside Home Assistant itself. Either way, a guided setup takes you from a new token to a working agent in minutes, backed by a catalog of 159 tools for reading, controlling, and authoring your configuration. The panel is available in English, Simplified Chinese, Traditional Chinese, and Japanese, following your Home Assistant profile language by default.

## Documentation

The full documentation, including the tool reference, permissions, capabilities, MESA, and the admin API, is at **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. The panel's help links point there.

New here? Start with the **[Quick start](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**: it creates your first token, grants it one device, then lets you choose whether to chat from inside Home Assistant or connect an external agent, in a few minutes either way.

## Requirements

- Home Assistant 2025.2.0 or later.
- One Phoenix MCP instance per Home Assistant. No Python dependencies beyond what HA ships.

## Install via HACS

1. In HACS, open **Integrations**, then the top-right menu, and choose **Custom repositories**.
2. Enter `https://github.com/leecaochang/phoenix-mcp` and select **Integration** as the category, then click **Add**.
3. Find Phoenix MCP in the HACS integration list, install it, and restart Home Assistant.

Prefer to install by hand? Copy the `custom_components/phoenix_mcp` folder into your Home Assistant config directory under `custom_components/phoenix_mcp`, then restart.

### Set up

Go to **Settings > Devices & services > Add integration** and search for **Phoenix MCP**. Click through the single-step config flow, then open the **Phoenix MCP** panel in your sidebar. The [Quick start](https://leecaochang.github.io/phoenix-mcp/quickstart.html) takes it from there.

## Issues and feedback

Report issues at [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
