# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[中文](#中文)

Phoenix MCP gives your AI agents scoped, least-privilege access to Home Assistant. Each client gets its own token, limited to exactly the entities you allow, with its own capabilities, rate limit, and optional expiry. Auditing is enabled by default and configurable per outcome, any token can be revoked instantly, and the per-entity semantic safety layer (MESA) can make a device confirm-only or off-limits by its nature, no matter what permissions a token is granted.

Phoenix MCP runs entirely inside Home Assistant, with no extra process, no cloud dependency in the core server, and no setup beyond the Phoenix MCP panel (the optional Agent Chat, voice, and AI Task features send conversations to the model provider you configure, unless you point them at a local Ollama). It works with the MCP clients you already use (Claude Code, Cursor, ChatGPT/Codex, Gemini, and others), or you can skip installing a client altogether and chat from inside Home Assistant itself. Either way, a guided setup takes you from a new token to a working agent in minutes, backed by a catalog of 143 tools for reading, controlling, and authoring your configuration. The panel is available in English and Simplified Chinese, following your Home Assistant profile language by default.

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

## 中文

Phoenix MCP 为您的 AI 智能体提供限定范围、最小权限的 Home Assistant 访问方式。每个客户端都拥有自己的令牌，只能访问您明确允许的实体，并各自拥有独立的能力、速率限制和可选的有效期。

审计默认开启，并可按结果类型分别配置；任何令牌都可以立即吊销；逐实体的语义安全层（MESA）还能根据设备本身的性质，将其设为仅可在确认后操作或完全禁止操作，无论令牌被授予了什么权限。

Phoenix MCP 完全运行在 Home Assistant 内部：无需额外进程，核心服务不依赖任何云服务，除 Phoenix MCP 面板之外也无需任何配置（可选的智能体对话、语音和 AI 任务功能会将对话发送给您配置的模型服务商，除非您将它们指向本地的 Ollama）。

它可以配合您已经在用的 MCP 客户端（Claude Code、Cursor、ChatGPT/Codex、Gemini 等），您也可以完全不安装客户端，直接在 Home Assistant 内部对话。无论采用哪种方式，引导式设置都能在几分钟内带您从新建令牌走到可用的智能体，背后是一个包含 143 个工具的目录，可用于读取、控制和编写您的配置。

面板提供英文和简体中文两种语言，默认跟随您的 Home Assistant 个人资料设置。

Phoenix MCP 脱胎于一个被弃置的开源项目，它的名字也由此而来。我最初只是把界面翻译成中文，后来又修复了大量 bug。目前项目的运行状态非常良好。

需要说明的是，我在开发中借助了大语言模型（LLM）。我目前的职业是高级软件工程师，所有由 LLM 生成的代码都经过我本人的人工审阅和冒烟测试。

很抱歉，文档还没有从英文翻译过来。这项工作量很大，但如果有足够多的人需要，我会着手去做。

我打算长期维护这个项目，也有不少新功能的构想。欢迎提交新的想法或 issue，我会尽快回复。
