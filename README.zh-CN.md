# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | **简体中文** | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

Phoenix MCP 为您的 AI 智能体提供限定范围、最小权限的 Home Assistant 访问方式。每个客户端都拥有自己的令牌，只能访问您明确允许的实体，并各自拥有独立的功能、速率限制和可选的有效期。审计默认开启，并可按结果类型分别配置；任何令牌都可以立即吊销；逐实体的语义安全层（MESA）还能根据设备本身的性质，将其设为仅可在确认后操作或完全禁止操作，无论令牌被授予了什么权限。

Phoenix MCP 完全运行在 Home Assistant 内部：无需额外进程，核心服务器不依赖任何云服务，除 Phoenix MCP 面板之外也无需任何设置（可选的智能体对话、语音和 AI 任务功能会将对话发送给您配置的模型提供商，除非您将它们指向本地的 Ollama）。它可以配合您已经在用的 MCP 客户端（Claude Code、Cursor、ChatGPT/Codex、Gemini 等），您也可以完全不安装客户端，直接在 Home Assistant 内部对话。无论采用哪种方式，引导式设置都能在几分钟内带您从新建令牌走到可用的智能体，背后是一个包含 159 个工具的目录，可用于读取、控制和编写您的配置。面板提供英文、简体中文、繁體中文、韩文和日文，默认跟随您的 Home Assistant 个人资料语言。

<!-- readme-i18n:locale-only:start -->

Phoenix MCP 脱胎于一个被弃置的开源项目，它的名字也由此而来。我最初只是把界面翻译成中文，后来又修复了大量 bug。目前项目的运行状态非常良好。

需要说明的是，我在开发中借助了大语言模型（LLM）。我目前的职业是高级软件工程师，所有由 LLM 生成的代码都经过我本人的人工审阅和冒烟测试。

很抱歉，文档还没有从英文翻译过来。这项工作量很大，但如果有足够多的人需要，我会着手去做。

我打算长期维护这个项目，也有不少新功能的构想。欢迎提交新的想法或 issue，我会尽快回复。

<!-- readme-i18n:locale-only:end -->

## 文档

完整文档包括工具参考、权限、功能、MESA 和管理 API，位于 **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**。面板中的帮助链接也会指向这里。

第一次使用？请从**[快速入门](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**开始：它会创建您的第一个令牌，为其授予一台设备的访问权限，然后让您选择直接在 Home Assistant 内对话或连接外部智能体。无论选择哪种方式，几分钟即可完成。

## 要求

- Home Assistant 2025.2.0 或更高版本。
- 每个 Home Assistant 只能配置一个 Phoenix MCP 实例。除 HA 自带的内容外，不需要其他 Python 依赖项。

## 通过 HACS 安装

1. 在 HACS 中打开**集成**，然后打开右上角菜单并选择**自定义存储库**。
2. 输入 `https://github.com/leecaochang/phoenix-mcp`，类别选择**集成**，然后点击**添加**。
3. 在 HACS 集成列表中找到 Phoenix MCP，安装后重新启动 Home Assistant。

希望手动安装？将 `custom_components/phoenix_mcp` 文件夹复制到 Home Assistant 配置目录下的 `custom_components/phoenix_mcp`，然后重新启动。

### 设置

前往**设置 > 设备与服务 > 添加集成**，搜索 **Phoenix MCP**。完成这个单步骤配置流程，然后打开侧边栏中的 **Phoenix MCP** 面板。之后请按照[快速入门](https://leecaochang.github.io/phoenix-mcp/quickstart.html)继续操作。

## 问题与反馈

请在 [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues) 报告问题。
