"""Canonicalize Home Assistant language tags to Phoenix catalog locales."""

from __future__ import annotations

_SIMPLIFIED_CHINESE_REGIONS = frozenset({"CN", "MY", "SG"})
_TRADITIONAL_CHINESE_REGIONS = frozenset({"HK", "MO", "TW"})

# Keep this list in step with catalogs/*.json. The public-language contract test
# checks that adding a catalog also updates this mapping without touching the
# request path or scanning the filesystem.
SHIPPED_LOCALES = frozenset({
    "de", "en", "es", "fr", "ja", "ko", "nl", "pl", "ru", "zh-Hans", "zh-Hant",
})


def _normalize(language: object) -> tuple[str, list[str]] | None:
    if not language:
        return None
    raw = str(language).strip().replace("_", "-")
    if not raw:
        return None
    parts = raw.split("-")
    if any(not part for part in parts):
        return None
    return parts[0].casefold(), parts[1:]


def canonical_language(language: object) -> str:
    """Return the closest shipped Phoenix locale, with English as last resort.

    Home Assistant normally uses the same script-aware codes as Phoenix, but
    browsers, operating-system settings, older profile data, and voice
    pipelines can provide regional BCP 47 tags instead. Exact matches win,
    then Chinese script or regional equivalents, then the base language.

    This function deliberately uses only in-memory constants. It runs on the
    Home Assistant event loop while serving catalog requests, so filesystem
    discovery here would create a blocking scandir warning.
    """
    normalized = _normalize(language)
    if normalized is None:
        return "en"

    base, subtags = normalized
    exact = "-".join([base, *subtags]).casefold()
    exact_match = next(
        (locale for locale in SHIPPED_LOCALES if locale.casefold() == exact),
        None,
    )
    if exact_match:
        return exact_match

    if base == "zh":
        script = next((part.title() for part in subtags if len(part) == 4), None)
        region = next(
            (part.upper() for part in subtags if len(part) in (2, 3) and part.isalnum()),
            None,
        )
        if script in ("Hans", "Hant") and f"zh-{script}" in SHIPPED_LOCALES:
            return f"zh-{script}"
        if region in _TRADITIONAL_CHINESE_REGIONS:
            return "zh-Hant"
        if region in _SIMPLIFIED_CHINESE_REGIONS or region is None:
            return "zh-Hans"

    base_match = next(
        (locale for locale in SHIPPED_LOCALES if locale.casefold() == base),
        None,
    )
    return base_match or "en"
