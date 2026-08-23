# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Nederlands](README.nl.md) | **Polski** | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Русский](README.ru.md)

Phoenix MCP daje Twoim agentom AI ograniczony dostęp do Home Assistant na zasadzie najmniejszych uprawnień. Każdy klient otrzymuje własny token, ograniczony dokładnie do encji, które zezwolisz, z własnymi możliwościami, limitem szybkości i opcjonalnym wygaśnięciem. Audyt jest domyślnie włączony i konfigurowalny dla każdego wyniku, każdy token można natychmiast unieważnić, a semantyczna warstwa bezpieczeństwa dla każdej encji (MESA) może sprawić, że urządzenie będzie wymagało potwierdzenia lub będzie niedostępne ze względu na swoją naturę, niezależnie od przyznanych tokenowi uprawnień.

Phoenix MCP działa w całości wewnątrz Home Assistant, bez dodatkowego procesu, bez zależności od chmury w głównym serwerze i bez konfiguracji wykraczającej poza panel Phoenix MCP (opcjonalne funkcje Czat agenta, głos i Zadanie AI wysyłają rozmowy do skonfigurowanego dostawcy modelu, chyba że skierujesz je do lokalnego Ollama). Działa z klientami MCP, których już używasz (Claude Code, Cursor, ChatGPT/Codex, Gemini i inne), lub możesz całkowicie pominąć instalowanie klienta i rozmawiać bezpośrednio z Home Assistant. W obu przypadkach konfiguracja z przewodnikiem prowadzi Cię od nowego tokena do działającego agenta w kilka minut, wspierana przez katalog 159 narzędzi do odczytywania, sterowania i tworzenia Twojej konfiguracji. Panel jest dostępny w wielu językach, domyślnie podąża za językiem Twojego profilu Home Assistant i przełącza się na angielski, gdy ten język jest niedostępny.

## Języki

Panel obsługuje obecnie angielski, hiszpański, francuski, niemiecki, niderlandzki, polski, rosyjski, chiński uproszczony, chiński tradycyjny, koreański i japoński. Aby ręcznie zmienić język panelu, otwórz **Phoenix MCP > Ustawienia > Język** i wybierz język. Wybierz **Auto**, aby ponownie podążać za językiem Twojego profilu Home Assistant.

## Dokumentacja

Pełna dokumentacja, w tym spis narzędzi, uprawnienia, możliwości, MESA i API administratora, znajduje się na **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. Prowadzą tam linki pomocy w panelu.

Nowy tutaj? Zacznij od **[Szybkiego startu](https://leecaochang.github.io/phoenix-mcp/quickstart.html)**: tworzy on Twój pierwszy token, przyznaje mu dostęp do jednego urządzenia, a następnie pozwala wybrać, czy chcesz rozmawiać z Home Assistant, czy połączyć zewnętrznego agenta; w obu przypadkach w kilka minut.

## Wymagania

- Home Assistant 2025.2.0 lub nowszy.
- Jedna instancja Phoenix MCP na Home Assistant. Brak dodatkowych zależności Python poza tymi dostarczanymi przez HA.

## Instalacja przez HACS

1. W HACS otwórz **Integracje**, następnie menu w prawym górnym rogu i wybierz **Repozytoria niestandardowe**.
2. Wpisz `https://github.com/leecaochang/phoenix-mcp`, wybierz **Integracja** jako kategorię, a następnie kliknij **Dodaj**.
3. Znajdź Phoenix MCP na liście integracji HACS, zainstaluj je i uruchom ponownie Home Assistant.

Wolisz zainstalować ręcznie? Skopiuj folder `custom_components/phoenix_mcp` do katalogu konfiguracji Home Assistant w `custom_components/phoenix_mcp`, a następnie uruchom ponownie.

### Konfiguracja

Przejdź do **Ustawienia > Urządzenia i usługi > Dodaj integrację** i wyszukaj **Phoenix MCP**. Przejdź przez jednoetapowy przepływ konfiguracji, a następnie otwórz panel **Phoenix MCP** na pasku bocznym. [Szybki start](https://leecaochang.github.io/phoenix-mcp/quickstart.html) poprowadzi Cię dalej.

## Problemy i opinie

Zgłaszaj problemy na [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
