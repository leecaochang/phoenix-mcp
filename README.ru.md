# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Nederlands](README.nl.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | **Русский**

Phoenix MCP даёт вашим ИИ-агентам ограниченный доступ к Home Assistant по принципу минимальных привилегий. Каждый клиент получает собственный токен, ограниченный ровно теми сущностями, которые вы разрешите, с собственными возможностями, ограничением частоты и необязательным сроком действия. Аудит включён по умолчанию и настраивается по каждому результату, любой токен можно мгновенно отозвать, а слой семантической безопасности для каждой сущности (MESA) может сделать устройство доступным только с подтверждением или полностью запрещённым по его природе, независимо от выданных токену разрешений.

Phoenix MCP полностью работает внутри Home Assistant: без дополнительного процесса, без облачной зависимости в основном сервере и без настройки, кроме панели Phoenix MCP (необязательные функции «Чат с агентом», голос и AI-задача отправляют диалоги настроенному вами провайдеру моделей, если вы не укажете локальный Ollama). Он работает с MCP-клиентами, которые вы уже используете (Claude Code, Cursor, ChatGPT/Codex, Gemini и другие), или можно вовсе не устанавливать клиент и общаться прямо из Home Assistant. В любом случае управляемая настройка за несколько минут проведёт вас от нового токена к работающему агенту, опираясь на каталог из 159 инструментов для чтения, управления и создания конфигурации. Панель доступна на нескольких языках, по умолчанию следует языку вашего профиля Home Assistant и переключается на английский, когда этот язык недоступен.

## Языки

Сейчас панель поддерживает английский, испанский, французский, немецкий, нидерландский, русский, упрощённый китайский, традиционный китайский, корейский и японский. Чтобы изменить язык панели вручную, откройте **Phoenix MCP > Настройки > Язык** и выберите язык. Выберите **Авто**, чтобы снова следовать языку вашего профиля Home Assistant.

## Документация

Полная документация, включая справочник инструментов, разрешения, возможности, MESA и административный API, находится по адресу **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. Ссылки справки в панели ведут туда.

Впервые здесь? Начните с **[Быстрого старта](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**: он создаёт ваш первый токен, предоставляет ему доступ к одному устройству, а затем позволяет выбрать, общаться ли из Home Assistant или подключить внешнего агента. В обоих случаях это занимает несколько минут.

## Требования

- Home Assistant 2025.2.0 или новее.
- Один экземпляр Phoenix MCP на Home Assistant. Никаких зависимостей Python сверх тех, что поставляет HA.

## Установка через HACS

1. В HACS откройте **Интеграции**, затем меню в правом верхнем углу и выберите **Пользовательские репозитории**.
2. Введите `https://github.com/leecaochang/phoenix-mcp`, выберите категорию **Интеграция** и нажмите **Добавить**.
3. Найдите Phoenix MCP в списке интеграций HACS, установите его и перезапустите Home Assistant.

Предпочитаете установить вручную? Скопируйте папку `custom_components/phoenix_mcp` в каталог конфигурации Home Assistant в `custom_components/phoenix_mcp`, затем перезапустите.

### Настройка

Перейдите в **Настройки > Устройства и службы > Добавить интеграцию** и найдите **Phoenix MCP**. Пройдите одношаговый поток настройки, затем откройте панель **Phoenix MCP** в боковой панели. Дальше вас проведёт [Быстрый старт](https://leecaochang.github.io/phoenix-mcp/quickstart.html).

## Проблемы и отзывы

Сообщайте о проблемах на [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
