"""Compatibility and restoration boundary for integration-aware logger control.

Home Assistant's public logger read reports effective levels but not whether an
integration override is runtime-only, one-restart, or permanent.  Keep that one
private read here, validate its shape every time, and fail closed on drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

_LOGGER = logging.getLogger(__name__)

LOGGER_CONTROL_RUNTIME_KEY = "phoenix_mcp_logger_control_runtime"
LOGGER_CONTROL_STORAGE_KEY = "phoenix_mcp.logger_timed_overrides"
LOGGER_CONTROL_STORAGE_VERSION = 1

VALID_LEVELS = frozenset({"NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
VALID_PERSISTENCE = frozenset({"none", "once", "permanent"})


class LoggerControlUnavailable(RuntimeError):
    """Home Assistant's logger state is absent or has changed shape."""


@dataclass(frozen=True, slots=True)
class IntegrationOverride:
    """One integration-aware logger override."""

    level: str
    persistence: str

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "persistence": self.persistence}


@dataclass(slots=True)
class TimedOverride:
    """Durable restoration record for one runtime-only override."""

    domain: str
    applied: IntegrationOverride
    prior: IntegrationOverride | None
    loggers: list[str]
    owner_token_id: str
    created_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "applied": self.applied.to_dict(),
            "prior": self.prior.to_dict() if self.prior else None,
            "loggers": list(self.loggers),
            "owner_token_id": self.owner_token_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class LoggerCompatibilityAdapter:
    """Narrow adapter over Home Assistant logger internals."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _settings_and_logs(self) -> tuple[Any, dict[str, Any]]:
        try:
            from homeassistant.components.logger.helpers import DATA_LOGGER
        except (ImportError, AttributeError) as exc:
            raise LoggerControlUnavailable("logger helper API is unavailable") from exc
        domain_config = self.hass.data.get(DATA_LOGGER)
        settings = getattr(domain_config, "settings", None)
        stored = getattr(settings, "_stored_config", None)
        if settings is None or not isinstance(stored, dict):
            raise LoggerControlUnavailable("logger state is unavailable")
        logs = stored.get("logs")
        if not isinstance(logs, dict):
            raise LoggerControlUnavailable("logger persistence state changed shape")
        return settings, logs

    def source_status(self) -> dict[str, Any]:
        try:
            _settings, logs = self._settings_and_logs()
            malformed = [domain for domain, value in logs.items() if not self._valid_setting(value)]
        except LoggerControlUnavailable as exc:
            return {
                "status": "degraded" if "logger" in self.hass.data else "unavailable",
                "warning": str(exc),
            }
        if malformed:
            return {
                "status": "degraded",
                "warning": "Some Home Assistant logger overrides have an unexpected shape.",
                "malformed_override_count": len(malformed),
            }
        return {"status": "available"}

    @staticmethod
    def _valid_setting(value: Any) -> bool:
        return (
            getattr(value, "level", None) in VALID_LEVELS
            and str(getattr(value, "persistence", "")) in VALID_PERSISTENCE
            and str(getattr(value, "type", "")) in ("integration", "module")
        )

    def get_override(self, domain: str) -> IntegrationOverride | None:
        _settings, logs = self._settings_and_logs()
        value = logs.get(domain)
        if value is None:
            return None
        if not self._valid_setting(value):
            raise LoggerControlUnavailable("integration logger override changed shape")
        if str(value.type) != "integration":
            # LoggerSettings has one namespace for module and integration keys.
            # Applying an integration setting here would overwrite an unrelated
            # module-level override and clearing it could not restore that type.
            raise LoggerControlUnavailable(
                "the integration domain is occupied by a module-level logger override"
            )
        return IntegrationOverride(
            level=str(value.level), persistence=str(value.persistence)
        )

    async def declared_loggers(self, domain: str) -> set[str]:
        try:
            from homeassistant.components.logger.helpers import get_integration_loggers

            loggers = await get_integration_loggers(self.hass, domain)
        except Exception as exc:  # noqa: BLE001 - compatibility boundary must fail closed
            raise LoggerControlUnavailable("integration logger discovery is unavailable") from exc
        if not isinstance(loggers, set) or not all(isinstance(item, str) for item in loggers):
            raise LoggerControlUnavailable("integration logger discovery changed shape")
        return loggers

    async def apply(self, domain: str, override: IntegrationOverride | None) -> None:
        self._settings_and_logs()
        try:
            from .ws_dispatch import async_ws_command

            desired = override or IntegrationOverride("NOTSET", "none")
            await async_ws_command(
                self.hass,
                "logger/integration_log_level",
                {
                    "integration": domain,
                    "level": desired.level,
                    "persistence": desired.persistence,
                },
            )
        except LoggerControlUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - compatibility boundary must fail closed
            raise LoggerControlUnavailable("integration logger update is unavailable") from exc

    @staticmethod
    def effective_levels(loggers: set[str] | list[str]) -> dict[str, str]:
        return {
            name: logging.getLevelName(logging.getLogger(name).getEffectiveLevel())
            for name in sorted(loggers)
        }


class LoggerOverrideManager:
    """Process-global timed restoration manager."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.adapter = LoggerCompatibilityAdapter(hass)
        self._store: Store[dict[str, Any]] = Store(
            hass, LOGGER_CONTROL_STORAGE_VERSION, LOGGER_CONTROL_STORAGE_KEY
        )
        self.records: dict[str, TimedOverride] = {}
        self._timers: dict[str, Any] = {}
        self.storage_available = True

    async def async_initialize(self) -> None:
        """Discard records from a prior HA process; runtime levels vanished too."""
        try:
            stale = await self._store.async_load()
            overrides = stale.get("overrides") if isinstance(stale, dict) else None
            if isinstance(overrides, dict) and overrides:
                _LOGGER.warning(
                    "Discarding stale Phoenix timed logger restoration records after Home Assistant restart"
                )
            if stale:
                await self._store.async_save({"overrides": {}})
        except Exception:  # noqa: BLE001 - optional control must not block Phoenix setup
            self.storage_available = False
            _LOGGER.exception("Phoenix timed logger restoration storage is unavailable")

    def active(self, domain: str) -> TimedOverride | None:
        return self.records.get(domain)

    async def _save(self) -> None:
        if not self.storage_available:
            return
        await self._store.async_save(
            {"overrides": {domain: record.to_dict() for domain, record in self.records.items()}}
        )

    def _cancel_timer(self, domain: str) -> None:
        cancel = self._timers.pop(domain, None)
        if cancel is not None:
            cancel()

    def _schedule(self, record: TimedOverride) -> None:
        self._cancel_timer(record.domain)
        delay = max(0.0, (record.expires_at - utcnow()).total_seconds())

        async def expire(_now: datetime) -> None:
            await self.async_expire(record.domain)

        self._timers[record.domain] = async_call_later(self.hass, delay, expire)

    async def async_set(
        self,
        *,
        domain: str,
        desired: IntegrationOverride | None,
        prior: IntegrationOverride | None,
        loggers: set[str],
        owner_token_id: str,
        duration_minutes: int | None,
    ) -> None:
        existing = self.records.get(domain)
        if duration_minutes is not None and not self.storage_available:
            raise LoggerControlUnavailable("timed logger restoration storage is unavailable")
        baseline = existing.prior if existing is not None else prior
        try:
            await self.adapter.apply(domain, desired)
        except Exception:
            # HA mutates its stored setting before resolving and applying the
            # logger set. A later failure may therefore be partial; make a
            # best-effort exact rollback before surfacing the error.
            try:
                await self.adapter.apply(domain, prior)
            except Exception:  # noqa: BLE001 - preserve the original failure
                _LOGGER.exception("Could not roll back partial logger update for %s", domain)
            raise
        if duration_minutes is None:
            self.records.pop(domain, None)
            try:
                await self._save()
            except Exception:
                if existing is not None:
                    self.records[domain] = existing
                await self.adapter.apply(domain, prior)
                raise
            self._cancel_timer(domain)
            return
        if desired is None:
            raise ValueError("a timed override requires an applied setting")
        now = utcnow()
        record = TimedOverride(
            domain=domain,
            applied=desired,
            prior=baseline,
            loggers=sorted(loggers),
            owner_token_id=owner_token_id,
            created_at=now,
            expires_at=now + timedelta(minutes=duration_minutes),
        )
        self.records[domain] = record
        try:
            await self._save()
        except Exception:
            if existing is None:
                self.records.pop(domain, None)
            else:
                self.records[domain] = existing
            await self.adapter.apply(domain, prior)
            raise
        self._schedule(record)

    async def async_expire(self, domain: str) -> None:
        record = self.records.get(domain)
        if record is None:
            return
        try:
            current = self.adapter.get_override(domain)
            loggers = await self.adapter.declared_loggers(domain)
            if current == record.applied and sorted(loggers) == record.loggers:
                await self.adapter.apply(domain, record.prior)
            else:
                _LOGGER.warning(
                    "Phoenix timed logger override for %s was superseded; not restoring", domain
                )
        except LoggerControlUnavailable:
            _LOGGER.exception("Could not safely restore timed logger override for %s", domain)
            # Keep the durable baseline and retry. Dropping it here would turn a
            # transient logger reload into an effectively permanent override.
            self._cancel_timer(domain)

            async def retry(_now: datetime) -> None:
                await self.async_expire(domain)

            self._timers[domain] = async_call_later(self.hass, 300, retry)
            return
        self._cancel_timer(domain)
        self.records.pop(domain, None)
        await self._save()

    async def async_restore_all(self) -> None:
        """Restore matching active overrides before Phoenix core data is wiped."""
        for domain in list(self.records):
            await self.async_expire(domain)
            # A wipe cannot retain Phoenix-owned restoration state. If HA's
            # logger surface is unavailable, async_expire scheduled a retry;
            # cancel it and remove the record after making the best safe effort.
            self._cancel_timer(domain)
            self.records.pop(domain, None)
        if self.storage_available:
            await self._store.async_save({"overrides": {}})


async def async_get_logger_override_manager(hass: HomeAssistant) -> LoggerOverrideManager:
    """Return the one manager for this HA process, surviving Phoenix reloads."""
    existing = hass.data.get(LOGGER_CONTROL_RUNTIME_KEY)
    if isinstance(existing, LoggerOverrideManager):
        return existing
    manager = LoggerOverrideManager(hass)
    await manager.async_initialize()
    hass.data[LOGGER_CONTROL_RUNTIME_KEY] = manager
    return manager
