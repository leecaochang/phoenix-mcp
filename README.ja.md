# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [简体中文](README.zh-CN.md) | **日本語**

Phoenix MCP は、AI エージェントに対して、範囲を限定した最小権限の Home Assistant アクセスを提供します。クライアントごとに専用のトークンが発行され、明示的に許可したエンティティだけにアクセスできます。各トークンには、個別の機能、レート制限、任意の有効期限を設定できます。監査はデフォルトで有効になっており、結果ごとに設定できます。どのトークンも即座に取り消せます。また、エンティティ単位のセマンティック安全レイヤー（MESA）により、トークンに付与された権限にかかわらず、デバイスの性質に応じて操作を確認必須または禁止にできます。

Phoenix MCP は Home Assistant 内で完結して動作します。追加のプロセスは不要で、コアサーバーはクラウドに依存せず、Phoenix MCP パネル以外の設定も必要ありません（オプションのエージェントチャット、音声、AI タスク機能では、ローカルの Ollama を指定しない限り、設定したモデルプロバイダーへ会話が送信されます）。Claude Code、Cursor、ChatGPT/Codex、Gemini など、すでにお使いの MCP クライアントと連携できます。クライアントをインストールせず、Home Assistant 内から直接チャットすることもできます。どちらの場合も、ガイド付きセットアップに従えば、新しいトークンの作成からエージェントの利用開始まで数分で完了します。設定の読み取り、制御、作成・編集に対応する 150 個のツールが用意されています。パネルは英語、簡体字中国語、日本語に対応し、デフォルトでは Home Assistant プロファイルの言語に従います。

## ドキュメント

ツールリファレンス、権限、機能、MESA、管理 API を含む完全なドキュメントは、**[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)** で公開しています。パネル内のヘルプリンクもこのサイトを参照します。

初めてお使いですか？まずは**[クイックスタート](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**をご覧ください。最初のトークンを作成し、一台のデバイスへのアクセスを許可した後、Home Assistant 内でチャットするか、外部エージェントを接続するかを選択できます。どちらの方法でも数分で完了します。

## 動作要件

- Home Assistant 2025.2.0 以降。
- Home Assistant ごとに Phoenix MCP インスタンスを一つだけ設定できます。HA に同梱されているもの以外に Python の依存関係はありません。

## HACS からインストール

1. HACS で**インテグレーション**を開き、右上のメニューから**カスタムリポジトリ**を選択します。
2. `https://github.com/leecaochang/phoenix-mcp` を入力し、カテゴリとして**インテグレーション**を選択して、**追加**をクリックします。
3. HACS のインテグレーション一覧で Phoenix MCP を見つけてインストールし、Home Assistant を再起動します。

手動でインストールする場合は、`custom_components/phoenix_mcp` フォルダーを Home Assistant の設定ディレクトリ内の `custom_components/phoenix_mcp` にコピーしてから、再起動してください。

### セットアップ

**設定 > デバイスとサービス > 統合を追加**を開き、**Phoenix MCP** を検索します。一段階の設定フローを完了したら、サイドバーから **Phoenix MCP** パネルを開いてください。その後は[クイックスタート](https://leecaochang.github.io/phoenix-mcp/quickstart.html)に沿って進めます。

## 問題の報告とフィードバック

問題は [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues) で報告してください。
