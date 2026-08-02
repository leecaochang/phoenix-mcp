"""TriggerValidator: live validation of triggers_automations declarations (Spec 5.5).

Cross-references profiles declaring ``triggers_automations: none`` against the
actual HA automation configurations supplied by the host. An entity declared
``none`` that appears in an automation trigger or condition block is a stale
and unsafe declaration: agents will skip cascade caution for it.

mesa-core never calls HA: the host provides automation configs through the
``get_automation_configs`` callback, from any source (REST API, YAML parse, or
test fixture). Automations can also reference entities indirectly, through
device triggers and the target selectors of purpose-specific triggers
(HA 2026.7+); resolving those requires the host's ``expand_target`` callback,
because only the host can query the HA registries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from custom_components.phoenix_mcp.mesa_core.exceptions import MesaValidationError
from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver
from custom_components.phoenix_mcp.mesa_core.profile import HA_AUTOMATION_SELECTOR_KEYS, TriggersAutomations
from custom_components.phoenix_mcp.mesa_core.store import ProfileStore

# HA configs use singular and plural section keys depending on age and editor.
_SECTION_KEYS = {
    "trigger": ("trigger", "triggers"),
    "condition": ("condition", "conditions"),
    "action": ("action", "actions"),
}

# Selector keys that reference entities indirectly (device triggers/conditions,
# and the target blocks of purpose-specific triggers). Only the host can
# resolve these against the HA registries.
_TARGET_KEYS = HA_AUTOMATION_SELECTOR_KEYS


@dataclass
class ValidationIssue:
    entity_id: str
    declared_value: str
    automation_id: str
    role: str  # "trigger", "condition", or "action"
    severity: str  # "error" or "warning"
    recommendation: str


def _collect_references(
    node: Any, entities: set[str], targets: set[tuple[str, str]]
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, str):
                    entities.add(value)
                elif isinstance(value, list):
                    entities.update(v for v in value if isinstance(v, str))
            elif key in _TARGET_KEYS:
                if isinstance(value, str):
                    targets.add((key, value))
                elif isinstance(value, list):
                    targets.update((key, v) for v in value if isinstance(v, str))
            else:
                _collect_references(value, entities, targets)
    elif isinstance(node, list):
        for item in node:
            _collect_references(item, entities, targets)


def entities_by_role(
    config: dict[str, Any],
    expand_target: Callable[[str, str], list[str]] | None = None,
) -> dict[str, set[str]]:
    """Entities referenced in an automation config, keyed by role.

    Returns a dict with keys ``"trigger"``, ``"condition"``, and ``"action"``,
    each mapping to the set of entity IDs referenced in that block. Handles the
    singular/plural HA section keys transparently. This is the canonical
    automation-config traversal; hosts building reverse-reference indexes
    should call this rather than reimplementing the entity-ID walk.

    ``expand_target`` resolves indirect references: it is called once per
    target selector found (``kind`` is one of ``area_id``, ``device_id``,
    ``floor_id``, ``label_id``; ``ref`` is the selector value) and returns the
    entity IDs that selector covers in the deployment. Without the callback,
    indirectly referenced entities are invisible to the walk.
    """
    result: dict[str, set[str]] = {}
    for role, keys in _SECTION_KEYS.items():
        entities: set[str] = set()
        targets: set[tuple[str, str]] = set()
        for key in keys:
            if key in config:
                _collect_references(config[key], entities, targets)
        if expand_target is not None:
            for kind, ref in targets:
                entities.update(expand_target(kind, ref))
        result[role] = entities
    return result

class TriggerValidator:
    def __init__(
        self,
        store: ProfileStore,
        *,
        expand_target: Callable[[str, str], list[str]] | None = None,
        resolver: InheritanceResolver | None = None,
    ) -> None:
        self.store = store
        self.expand_target = expand_target
        self.resolver = resolver or InheritanceResolver(store=store)

    def _is_none(self, entity_id: str) -> bool:
        """Whether an entity reads as ``triggers_automations: none`` to an agent.

        Resolved, not stored: agents consume the effective profile, so a `none`
        inherited from a domain, integration, area, or device profile, or from
        deployment defaults, skips cascade caution exactly as an entity-level
        one does and is just as stale if the entity is in fact a trigger.
        """
        try:
            effective = self.resolver.resolve(entity_id)
        except MesaValidationError:
            return False
        return (
            effective.operational_boundaries.triggers_automations == TriggersAutomations.NONE
        )

    def _declared_none_entities(self, entity_ids: Iterable[str] | None = None) -> list[str]:
        """Entities that resolve to ``none``.

        ``entity_ids`` is the host's entity registry. Without it only entities
        carrying their own stored profile can be enumerated, so an entity that
        inherits `none` from a broader profile and has no profile of its own is
        invisible to the check.
        """
        candidates = list(entity_ids) if entity_ids is not None else self.store.entity_keys()
        return [key for key in candidates if self._is_none(key)]

    def _walked_configs(
        self, configs: list[dict[str, Any]]
    ) -> list[tuple[str, dict[str, set[str]]]]:
        """Walk each config (and expand its target selectors) exactly once.

        The walk and the ``expand_target`` registry calls are per config, not
        per entity-config pair: with hundreds of ``none`` declarations the
        re-expansion dominated ``validate()``.
        """
        return [
            (str(config.get("id", "<unknown>")), entities_by_role(config, self.expand_target))
            for config in configs
        ]

    def _issues_for(
        self, entity_id: str, walked: list[tuple[str, dict[str, set[str]]]]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for automation_id, by_role in walked:
            # Only trigger and condition references invalidate a none declaration
            # (Spec 5.5): an entity written by an action does not trigger automations.
            for role, severity in (("trigger", "error"), ("condition", "warning")):
                if entity_id in by_role[role]:
                    issues.append(
                        ValidationIssue(
                            entity_id=entity_id,
                            declared_value="none",
                            automation_id=automation_id,
                            role=role,
                            severity=severity,
                            recommendation=(
                                f"{entity_id} is declared triggers_automations: none but "
                                f"appears in the {role} block of {automation_id}. "
                                "Change the declaration to 'likely', or to "
                                "'deployment_defined' with affected_automations listing "
                                "this automation."
                            ),
                        )
                    )
        return issues

    def validate(
        self,
        get_automation_configs: Callable[[], list[dict[str, Any]]],
        *,
        entity_ids: Iterable[str] | None = None,
    ) -> list[ValidationIssue]:
        """Cross-reference every effective ``none`` against the automation registry.

        ``entity_ids`` is the host's entity registry. Hosts SHOULD pass it: an
        entity that inherits ``none`` from a domain, integration, or area
        profile without carrying one of its own is not in the store's key set,
        so it can only be checked when the host names it.
        """
        walked = self._walked_configs(get_automation_configs())
        issues: list[ValidationIssue] = []
        for entity_id in self._declared_none_entities(entity_ids):
            issues.extend(self._issues_for(entity_id, walked))
        return issues

    def validate_entity(
        self,
        entity_id: str,
        get_automation_configs: Callable[[], list[dict[str, Any]]],
    ) -> list[ValidationIssue]:
        """Validate a single entity against the automation registry."""
        if not self._is_none(entity_id):
            return []
        return self._issues_for(entity_id, self._walked_configs(get_automation_configs()))

    async def avalidate(
        self,
        get_automation_configs: Callable[[], list[dict[str, Any]]],
        *,
        entity_ids: Iterable[str] | None = None,
    ) -> list[ValidationIssue]:
        return await asyncio.to_thread(
            lambda: self.validate(get_automation_configs, entity_ids=entity_ids)
        )

    async def avalidate_entity(
        self,
        entity_id: str,
        get_automation_configs: Callable[[], list[dict[str, Any]]],
    ) -> list[ValidationIssue]:
        return await asyncio.to_thread(
            self.validate_entity, entity_id, get_automation_configs
        )
