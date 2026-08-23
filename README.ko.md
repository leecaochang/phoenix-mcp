# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | **한국어**

Phoenix MCP는 AI 에이전트에 범위가 제한된 최소 권한의 Home Assistant 접근을 제공합니다. 각 클라이언트는 고유한 토큰을 가지며, 허용한 엔티티로만 제한되고, 개별 기능, 속도 제한, 선택적 만료 기간을 갖습니다. 감사는 기본적으로 활성화되어 있으며 결과별로 구성할 수 있고, 모든 토큰은 즉시 철회할 수 있습니다. 엔티티별 시맨틱 안전 계층(MESA)은 토큰에 부여된 권한과 관계없이 기기의 특성에 따라 확인 전용 또는 접근 금지로 만들 수 있습니다.

Phoenix MCP는 Home Assistant 내부에서 완전히 실행됩니다. 추가 프로세스가 필요 없고, 핵심 서버에 클라우드 의존성이 없으며, Phoenix MCP 패널 외에는 별도 설정이 필요 없습니다(선택 사항인 에이전트 채팅, 음성, AI 작업 기능은 로컬 Ollama를 지정하지 않는 한, 구성한 모델 공급자에게 대화를 보냅니다). 이미 사용 중인 MCP 클라이언트(Claude Code, Cursor, ChatGPT/Codex, Gemini 등)와 함께 사용할 수 있으며, 클라이언트를 설치하지 않고 Home Assistant 내부에서 바로 채팅할 수도 있습니다. 어느 쪽이든 안내식 설정을 통해 몇 분 안에 새 토큰에서 작동하는 에이전트까지 도달할 수 있으며, 구성을 읽고 제어하고 작성하는 159개 도구 카탈로그가 뒷받침합니다. 패널은 영어, 중국어 간체, 중국어 번체, 한국어, 일본어로 제공되며 기본적으로 Home Assistant 프로필 언어를 따릅니다.

<!-- readme-i18n:locale-only:start -->

Phoenix MCP는 버려진 오픈 소스 프로젝트에서 비롯되었으며 이름도 여기서 유래했습니다. 저는 처음에 인터페이스를 중국어로 번역하는 것에서 시작했고, 이후 많은 버그를 수정했습니다. 현재 프로젝트는 매우 안정적으로 운영되고 있습니다.

참고로 개발 과정에서 대규모 언어 모델(LLM)을 활용했습니다. 저는 현재 시니어 소프트웨어 엔지니어로 일하고 있으며, LLM이 생성한 모든 코드는 제가 직접 검토하고 스모크 테스트를 거쳤습니다.

죄송하지만 문서는 아직 영어에서 번역되지 않았습니다. 작업량이 많지만, 필요로 하는 분이 충분히 많다면 진행하겠습니다.

이 프로젝트를 장기적으로 유지 관리할 계획이며 새로운 기능에 대한 아이디어도 많습니다. 새로운 아이디어나 이슈를 환영하며, 최대한 빨리 답변하겠습니다.

<!-- readme-i18n:locale-only:end -->

## 문서

도구 레퍼런스, 권한, 기능, MESA, 관리 API를 포함한 전체 문서는 **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**에 있습니다. 패널의 도움말 링크도 이곳을 가리킵니다.

처음이신가요? **[빠른 시작](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**부터 시작하세요. 첫 토큰을 만들고 기기 하나에 권한을 부여한 다음, Home Assistant 내부에서 채팅할지 외부 에이전트를 연결할지 선택할 수 있습니다. 어느 쪽이든 몇 분이면 됩니다.

## 요구 사항

- Home Assistant 2025.2.0 이상.
- Home Assistant마다 Phoenix MCP 인스턴스 하나만 설정할 수 있습니다. HA가 제공하는 것 외에는 Python 의존성이 없습니다.

## HACS를 통한 설치

1. HACS에서 **통합 구성 요소**를 열고 오른쪽 상단 메뉴에서 **사용자 지정 저장소**를 선택하세요.
2. `https://github.com/leecaochang/phoenix-mcp`를 입력하고 범주로 **통합 구성 요소**를 선택한 다음 **추가**를 클릭하세요.
3. HACS 통합 구성 요소 목록에서 Phoenix MCP를 찾아 설치한 후 Home Assistant를 재시작하세요.

수동으로 설치하고 싶으신가요? `custom_components/phoenix_mcp` 폴더를 Home Assistant 설정 디렉터리의 `custom_components/phoenix_mcp` 아래에 복사한 후 재시작하세요.

### 설정

**설정 > 기기 및 서비스 > 통합 구성 요소 추가**로 이동하여 **Phoenix MCP**를 검색하세요. 단일 단계 구성 흐름을 완료한 다음 사이드바에서 **Phoenix MCP** 패널을 여세요. 이후에는 [빠른 시작](https://leecaochang.github.io/phoenix-mcp/quickstart.html)을 따르면 됩니다.

## 문제 및 피드백

문제는 [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues)에서 보고해 주세요.
