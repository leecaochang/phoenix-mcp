# v1.2.0

## Added

* Add Agent Chat shortcut and pop-out window support.
* Add permission-scoped device registry reads.
* Add reversible device registry updates.
* Add permission-scoped integration registry reads.
* Add safe integration lifecycle tools.
* Add safe integration removal.
* Add integration-aware device removal.
* Add permission-scoped entity registry updates with inherited access for registry-only entities.
* Add reversible entity registry operations with alias resolution in update responses.
* Add scoped integration log-level control.
* Add redesigned logbook diagnostics.
* Add redesigned system log diagnostics.
* Add redesigned recorder history and statistics tools.
* Add multimodal camera image support.
* Add Mistral AI provider support.
* Add Japanese interface and documentation translations.

## Changed

* Refactor tool catalog inputs for the catalog v2 contract.
* Close remaining catalog v2 contract gaps.
* Remove the token-facing REST proxy and use scoped native tools instead.
* Replace recorder REST endpoints with scoped recorder tools.
* Expand multilingual interface and documentation coverage.
* Enforce operator localization and translated-string contracts.
* Add friendly summaries for approval requests and results.
* Use the Home Assistant timezone for Agent Chat responses and timestamps.
* Improve Agent Chat provider capability discovery and model handling.
* Improve Agent Chat window behavior, authentication, and audit source reporting.
* Harden entity registry identity operations and include-graph handling.

## Fixed

* Prevent redacted dashboard layouts from being saved.
* Reject invalid entity registry edits before requesting approval.
* Fix browser Agent Chat bootstrap and authentication.
* Cover edits to legacy dashboard paths.
* Harden audit security and MCP protocol boundaries.
