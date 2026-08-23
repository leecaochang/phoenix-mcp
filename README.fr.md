# Phoenix MCP

[![HACS](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/hassfest.yml)
[![Tests](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/leecaochang/phoenix-mcp/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io)


[English](README.md) | [Español](README.es.md) | **Français** | [Deutsch](README.de.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-Hant.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

Phoenix MCP offre à vos agents IA un accès à Home Assistant limité par périmètre et selon le principe du moindre privilège. Chaque client reçoit son propre jeton, limité exactement aux entités que vous autorisez, avec ses propres capacités, limite de débit et date d'expiration facultative. L'audit est activé par défaut et configurable par résultat, tout jeton peut être révoqué instantanément, et la couche de sécurité sémantique par entité (MESA) peut imposer une confirmation ou interdire l'accès à un appareil selon sa nature, quels que soient les droits accordés au jeton.

Phoenix MCP s'exécute entièrement dans Home Assistant, sans processus supplémentaire, sans dépendance au cloud pour le serveur principal et sans configuration au-delà du panneau Phoenix MCP. Les fonctions facultatives Chat de l'agent, vocales et Tâche d'IA envoient les conversations au fournisseur de modèle que vous configurez, sauf si vous les dirigez vers un Ollama local. Il fonctionne avec les clients MCP que vous utilisez déjà, notamment Claude Code, Cursor, ChatGPT/Codex et Gemini. Vous pouvez aussi ne pas installer de client et discuter directement dans Home Assistant. Dans les deux cas, une configuration guidée vous fait passer d'un nouveau jeton à un agent opérationnel en quelques minutes, avec un catalogue de 159 outils pour lire, contrôler et créer votre configuration. Le panneau est disponible dans plusieurs langues, suit par défaut la langue de votre profil Home Assistant et utilise l'anglais si cette langue n'est pas disponible.

## Langues

Le panneau prend actuellement en charge l'anglais, l'espagnol, le français, l'allemand, le chinois simplifié, le chinois traditionnel, le coréen et le japonais. Pour changer manuellement la langue du panneau, ouvrez **Phoenix MCP > Paramètres > Langue** et choisissez une langue. Choisissez **Auto** pour suivre à nouveau la langue de votre profil Home Assistant.

## Documentation

La documentation complète, y compris la référence des outils, les autorisations, les capacités, MESA et l'API d'administration, est disponible sur **[leecaochang.github.io/phoenix-mcp](https://leecaochang.github.io/phoenix-mcp/)**. Les liens d'aide du panneau y renvoient.

Vous débutez ? Commencez par le **[Démarrage rapide](https://leecaochang.github.io/phoenix-mcp/quickstart.html)** : il crée votre premier jeton, lui accorde l'accès à un appareil, puis vous laisse choisir entre discuter dans Home Assistant ou connecter un agent externe, le tout en quelques minutes.

## Prérequis

- Home Assistant 2025.2.0 ou version ultérieure.
- Une instance Phoenix MCP par Home Assistant. Aucune dépendance Python au-delà de celles fournies par HA.

## Installation avec HACS

1. Dans HACS, ouvrez **Intégrations**, puis le menu en haut à droite, et choisissez **Dépôts personnalisés**.
2. Saisissez `https://github.com/leecaochang/phoenix-mcp`, sélectionnez **Intégration** comme catégorie, puis cliquez sur **Ajouter**.
3. Trouvez Phoenix MCP dans la liste des intégrations HACS, installez-le, puis redémarrez Home Assistant.

Vous préférez une installation manuelle ? Copiez le dossier `custom_components/phoenix_mcp` dans le répertoire de configuration de Home Assistant, sous `custom_components/phoenix_mcp`, puis redémarrez.

### Configuration

Ouvrez **Paramètres > Appareils et services > Ajouter une intégration** et recherchez **Phoenix MCP**. Terminez le flux de configuration en une étape, puis ouvrez le panneau **Phoenix MCP** dans la barre latérale. Le [Démarrage rapide](https://leecaochang.github.io/phoenix-mcp/quickstart.html) vous guidera ensuite.

## Problèmes et retours

Signalez les problèmes sur [github.com/leecaochang/phoenix-mcp/issues](https://github.com/leecaochang/phoenix-mcp/issues).
