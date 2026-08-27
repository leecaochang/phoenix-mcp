# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | **Nederlands** | [Polski](README.pl.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Русский](README.ru.md)

Phoenix MCP geeft je AI-agents afgebakende toegang tot Home Assistant volgens het principe van minimale rechten. Elke client krijgt een eigen token, beperkt tot precies de entiteiten die je toestaat, met eigen mogelijkheden, snelheidslimiet en optionele verloopdatum. Auditing is standaard ingeschakeld en per uitkomst configureerbaar, elk token kan direct worden ingetrokken, en de semantische veiligheidslaag per entiteit (MESA) kan een apparaat op basis van zijn aard op alleen-bevestigen of verboden zetten, ongeacht de machtigingen die een token krijgt.

Phoenix MCP draait volledig binnen Home Assistant, zonder extra proces, zonder cloudafhankelijkheid in de kernserver en zonder verdere installatie dan het Phoenix MCP-paneel (de optionele functies Agent Chat, spraak en AI-taak sturen gesprekken naar de modelprovider die je configureert, tenzij je ze naar een lokale Ollama verwijst). Het werkt met de MCP-clients die je al gebruikt (Claude Code, Cursor, ChatGPT/Codex, Gemini en andere), of je kunt het installeren van een client helemaal overslaan en rechtstreeks vanuit Home Assistant chatten. Hoe dan ook brengt een begeleide installatie je in enkele minuten van een nieuw token naar een werkende agent, ondersteund door een catalogus van 160 tools voor het lezen, besturen en opstellen van je configuratie. Het paneel is in meerdere talen beschikbaar, volgt standaard de taal van je Home Assistant-profiel en valt terug op Engels wanneer die taal niet beschikbaar is.

## Talen

Het paneel ondersteunt momenteel Engels, Spaans, Frans, Duits, Nederlands, Pools, Russisch, vereenvoudigd Chinees, traditioneel Chinees, Koreaans en Japans. Om de paneeltaal handmatig te wijzigen, open je **Phoenix MCP > Instellingen > Taal** en kies je een taal. Kies **Auto** om weer de taal van je Home Assistant-profiel te volgen.

## Documentatie

De volledige documentatie, inclusief de toolreferentie, machtigingen, mogelijkheden, MESA en de admin-API, staat op **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. De helplinks van het paneel verwijzen daarheen.

Nieuw hier? Begin met de **[Snelstart](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**: hiermee maak je je eerste token, geef je het toegang tot één apparaat en kies je vervolgens of je vanuit Home Assistant wilt chatten of een externe agent wilt verbinden; beide binnen enkele minuten.

## Vereisten

- Home Assistant 2025.2.0 of nieuwer.
- Eén Phoenix MCP-instantie per Home Assistant. Geen Python-afhankelijkheden buiten wat HA meelevert.

## Installeren via HACS

1. Open in HACS **Integraties**, vervolgens het menu rechtsboven en kies **Aangepaste repositories**.
2. Voer `https://github.com/leecaochang/phoenix-mcp` in, selecteer **Integratie** als categorie en klik op **Toevoegen**.
3. Zoek Phoenix MCP in de HACS-integratielijst, installeer het en herstart Home Assistant.

Liever handmatig installeren? Kopieer de map `custom_components/phoenix_mcp` naar de Home Assistant-configuratiemap onder `custom_components/phoenix_mcp` en herstart.

### Instellen

Ga naar **Instellingen > Apparaten en services > Integratie toevoegen** en zoek naar **Phoenix MCP**. Doorloop de configuratiestap en open vervolgens het **Phoenix MCP**-paneel in de zijbalk. De [Snelstart](https://leecaochang.github.io/phoenix-mcp/quickstart.html) neemt het vanaf daar over.

## Problemen en feedback

Meld problemen op [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
