# 安全政策

## 报告安全漏洞

如果您认为 Phoenix MCP 存在安全漏洞，请不要创建公开 Issue，也不要在公开讨论中披露细节。请使用 GitHub 的私密漏洞报告功能提交报告：

<https://github.com/leecaochang/phoenix-mcp/security/advisories/new>

报告应尽量包含以下信息：

- 受影响的版本、提交或安装方式。
- 可复现问题的最小步骤、请求示例或测试用例。
- 影响范围，包括机密性、完整性或可用性方面的影响。
- 在安全可行的情况下提供修复建议。

请删除真实的 Home Assistant 令牌、密码、个人数据、设备地址和其他敏感信息。可以使用经过编辑的配置、占位符和最小化的日志来复现问题。

我们会在 5 个工作日内确认收到报告，并在调查期间提供进展更新。我们会与报告者协调修复、发布版本和披露时间。我们不会要求报告者先公开漏洞，也不会因为善意的安全研究而追究责任。

## 范围

本政策适用于本仓库中的 Phoenix MCP 源代码、前端资源、构建配置和发布内容。Home Assistant 核心、其他集成、上游依赖以及托管环境中的问题，请同时向相应的上游项目报告。

本项目目前不提供漏洞赏金。提交报告不会产生报酬或其他奖励承诺。

## 支持版本

我们通常优先修复默认分支上的问题，以及最新的公开版本。无法继续支持的旧版本可能只会获得修复建议，而不会获得回溯修复。

## 安全更新

确认后的漏洞会根据严重程度和修复可用性安排修复。需要用户采取行动时，我们会在发布说明、GitHub 安全公告或其他适当渠道中说明受影响版本和升级步骤。

---

# Security Policy

## Reporting a Vulnerability

If you believe Phoenix MCP has a security vulnerability, please do not open a public issue or disclose the details in a public discussion. Use GitHub's private vulnerability reporting feature:

<https://github.com/leecaochang/phoenix-mcp/security/advisories/new>

Please include, where possible:

- The affected version, commit, or installation method.
- Minimal reproduction steps, request examples, or a test case.
- The impact, including any effect on confidentiality, integrity, or availability.
- A suggested fix, when it is safe and practical to provide one.

Please remove real Home Assistant tokens, passwords, personal data, device addresses, and other sensitive information. Redacted configuration, placeholders, and minimized logs are preferred for reproduction.

We will acknowledge a report within 5 business days and provide progress updates during the investigation. We will coordinate remediation, release, and disclosure timing with the reporter. We will not require a reporter to disclose a vulnerability publicly first, and we will not pursue good-faith security research.

## Scope

This policy covers the Phoenix MCP source code, frontend assets, build configuration, and release content in this repository. Issues in Home Assistant core, other integrations, upstream dependencies, or the hosting environment should also be reported to the appropriate upstream project.

This project does not currently offer a bug bounty. A report does not create an entitlement to payment or another reward.

## Supported Versions

We generally prioritize issues on the default branch and in the latest public release. Older versions that are no longer supportable may receive remediation guidance rather than a backported fix.

## Security Updates

Confirmed vulnerabilities will be scheduled for remediation based on severity and the availability of a fix. When users need to take action, release notes, GitHub security advisories, or another appropriate channel will identify affected versions and upgrade steps.
