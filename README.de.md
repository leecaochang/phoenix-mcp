# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [Español](README.es.md) | [Français](README.fr.md) | **Deutsch** | [Nederlands](README.nl.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Русский](README.ru.md)

Phoenix MCP gibt Ihren KI-Agenten einen bereichsbezogenen Zugriff auf Home Assistant nach dem Prinzip der minimalen Berechtigungen. Jeder Client erhält ein eigenes Token, das genau auf die von Ihnen erlaubten Entitäten beschränkt ist und über eigene Fähigkeiten, ein Ratenlimit sowie eine optionale Ablaufzeit verfügt. Die Prüfung ist standardmäßig aktiviert und je Ergebnis konfigurierbar. Jedes Token kann sofort widerrufen werden. Die semantische Sicherheitsschicht pro Entität (MESA) kann ein Gerät abhängig von seiner Art auf bestätigungspflichtig setzen oder sperren, unabhängig von den Berechtigungen des Tokens.

Phoenix MCP läuft vollständig in Home Assistant, ohne zusätzlichen Prozess, ohne Cloud-Abhängigkeit im Kernserver und ohne Einrichtung über das Phoenix MCP-Bedienfeld hinaus. Die optionalen Funktionen Agent-Chat, Sprache und KI-Aufgabe senden Unterhaltungen an den von Ihnen konfigurierten Modellanbieter, sofern Sie sie nicht auf ein lokales Ollama verweisen. Es funktioniert mit den MCP-Clients, die Sie bereits verwenden, darunter Claude Code, Cursor, ChatGPT/Codex, Gemini und weitere. Sie können auch ganz auf die Installation eines Clients verzichten und direkt in Home Assistant chatten. In beiden Fällen führt Sie eine geführte Einrichtung in wenigen Minuten von einem neuen Token zu einem funktionsfähigen Agenten. Ein Katalog mit 159 Werkzeugen unterstützt Sie beim Lesen, Steuern und Erstellen Ihrer Konfiguration. Das Bedienfeld ist in mehreren Sprachen verfügbar, folgt standardmäßig der Sprache Ihres Home Assistant-Profils und fällt auf Englisch zurück, wenn diese Sprache nicht verfügbar ist.

## Sprachen

Das Bedienfeld unterstützt derzeit Englisch, Spanisch, Französisch, Deutsch, Niederländisch, Russisch, vereinfachtes Chinesisch, traditionelles Chinesisch, Koreanisch und Japanisch. Um die Sprache des Bedienfelds manuell zu ändern, öffnen Sie **Phoenix MCP > Einstellungen > Sprache** und wählen Sie eine Sprache aus. Wählen Sie **Auto**, um wieder der Sprache Ihres Home Assistant-Profils zu folgen.

## Dokumentation

Die vollständige Dokumentation, einschließlich Werkzeugreferenz, Berechtigungen, Fähigkeiten, MESA und der Admin-API, finden Sie unter **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. Die Hilfe-Links des Bedienfelds führen dorthin.

Neu hier? Beginnen Sie mit dem **[Schnellstart](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**: Er erstellt Ihr erstes Token, gewährt ihm Zugriff auf ein Gerät und lässt Sie anschließend wählen, ob Sie direkt in Home Assistant chatten oder einen externen Agenten verbinden möchten. Beides dauert nur wenige Minuten.

## Voraussetzungen

- Home Assistant 2025.2.0 oder neuer.
- Eine Phoenix MCP-Instanz pro Home Assistant. Keine Python-Abhängigkeiten über die von HA bereitgestellten hinaus.

## Installation über HACS

1. Öffnen Sie in HACS **Integrationen**, dann das Menü oben rechts, und wählen Sie **Benutzerdefinierte Repositorys**.
2. Geben Sie `https://github.com/leecaochang/phoenix-mcp` ein, wählen Sie **Integration** als Kategorie und klicken Sie dann auf **Hinzufügen**.
3. Suchen Sie Phoenix MCP in der HACS-Integrationsliste, installieren Sie es und starten Sie Home Assistant neu.

Möchten Sie lieber manuell installieren? Kopieren Sie den Ordner `custom_components/phoenix_mcp` in das Home-Assistant-Konfigurationsverzeichnis unter `custom_components/phoenix_mcp` und starten Sie Home Assistant neu.

### Einrichtung

Öffnen Sie **Einstellungen > Geräte & Dienste > Integration hinzufügen** und suchen Sie nach **Phoenix MCP**. Durchlaufen Sie den einstufigen Einrichtungsablauf und öffnen Sie anschließend das **Phoenix MCP**-Bedienfeld in der Seitenleiste. Der [Schnellstart](https://leecaochang.github.io/phoenix-mcp/quickstart.html) führt Sie von dort weiter.

## Probleme und Feedback

Melden Sie Probleme unter [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
