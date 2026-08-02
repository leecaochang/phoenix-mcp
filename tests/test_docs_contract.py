"""Pin the enumerable parts of the docs site against the code.

Nearly all documentation drift is of one class: enumerable facts (tool
parameters, provider kinds, error codes, SSE frame names, routes, capability
lists) restated by hand and left behind by code changes. Everything here derives
the expectation from the code side, so a tool, provider, code, frame, route, or
capability added tomorrow is covered without a new test being written.

Extraction-based checks assert a floor on how much they extracted, so a
refactor that breaks the extraction fails loudly instead of passing vacuously
(the mutation-pin lesson: absence assertions are the likeliest to be vacuous).

`docs/` at the repo root is the only copy of the documentation. It is published
as a hosted site; the integration ships no copy of it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from custom_components.phoenix_mcp.const import (
    AGENTCLI_PROVIDERS,
    AI_TASK_MIN_HA,
    ASSIST_API_MIN_HA,
    CAPABILITY_NAMES,
)
from custom_components.phoenix_mcp.mcp_view import (
    _ENTITY_TOOL_DEFS,
    _NATIVE_TOOL_DEFS,
    _SYSTEM_TOOL_DEFS,
    tool_catalog_counts,
)
from custom_components.phoenix_mcp.mesa_tools import mesa_tool_defs

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PKG = ROOT / "custom_components" / "phoenix_mcp"


def _all_defs() -> list[dict]:
    return list(_ENTITY_TOOL_DEFS) + list(_NATIVE_TOOL_DEFS) + list(_SYSTEM_TOOL_DEFS) + list(mesa_tool_defs())


def _tools_articles() -> dict[str, str]:
    """Map tool name -> the <article> block documenting it.

    An article may document several tools (the ESPHome firmware family shares
    one), so multiple names can map to the same block.
    """
    html = (DOCS / "tools.html").read_text(encoding="utf-8")
    articles: dict[str, str] = {}
    for block in re.findall(r"<article\b.*?</article>", html, re.DOTALL):
        for name in re.findall(r'<span class="tool-name">(\w+)</span>', block):
            articles[name] = block
    return articles


def test_every_tool_has_an_article() -> None:
    articles = _tools_articles()
    missing = sorted({d["name"] for d in _all_defs()} - set(articles))
    assert missing == [], f"tools.html has no article for: {missing}"
    assert len(articles) >= tool_catalog_counts()["total"]


def test_every_required_param_is_named_in_its_article() -> None:
    """A parameter the schema REQUIRES must appear in the tool's article.

    The audit found 28 tools whose article never named at least one required
    field; an agent (or human) reading the docs could not produce a valid call.
    """
    articles = _tools_articles()
    gaps: list[str] = []
    for d in _all_defs():
        required = (d.get("inputSchema") or {}).get("required") or []
        block = articles.get(d["name"], "")
        for param in required:
            if not re.search(rf"\b{re.escape(param)}\b", block):
                gaps.append(f"{d['name']}: required param '{param}' not in its article")
    assert gaps == [], "\n".join(gaps)


def test_required_params_are_marked_required_in_single_tool_articles() -> None:
    """Naming a required parameter is not enough; the article must SAY it is required.

    Mentioning the name passed while the ESPHome firmware article listed `file`
    and `job_id` with no indication that each is mandatory for its own subset of
    tools, which is the exact thing a caller needs to know. Scoped to articles
    documenting exactly ONE tool: a shared article covers several tools whose
    required sets differ, so "required" cannot be attributed mechanically there
    and the prose has to carry it (the /docs-verify pass covers those).
    """
    articles = _tools_articles()
    shared = {block for name, block in articles.items()
              if sum(1 for n in articles if articles[n] is block) > 1}
    gaps: list[str] = []
    for d in _all_defs():
        required = (d.get("inputSchema") or {}).get("required") or []
        block = articles.get(d["name"], "")
        if not required or block in shared:
            continue
        # Tags become SPACES, not nothing: "<dt>params</dt><dd><code>entity_id"
        # would otherwise collapse to "paramsentity_id" and lose the word
        # boundary. A tag-aware window is wrong too, since "<code>domain</code>
        # and <code>service</code> (both required)" is a correct marking whose
        # markup would read as distance.
        text = re.sub(r"<[^>]+>", " ", block)
        for param in required:
            # The marker must sit in the same sentence, within a short window
            # after the parameter, so an unrelated "required" elsewhere in the
            # article cannot satisfy it.
            if not re.search(rf"\b{re.escape(param)}\b[^.]{{0,60}}required", text):
                gaps.append(
                    f"{d['name']}: '{param}' is required by the schema but its article"
                    " never marks it required"
                )
    assert gaps == [], "\n".join(gaps)


def test_admin_api_documents_every_capability() -> None:
    html = (DOCS / "admin-api.html").read_text(encoding="utf-8")
    missing = sorted(c for c in CAPABILITY_NAMES if c not in html)
    assert missing == [], f"admin-api.html PATCH schema missing caps: {missing}"


def test_capabilities_page_documents_every_capability() -> None:
    html = (DOCS / "capabilities.html").read_text(encoding="utf-8")
    missing = sorted(c for c in CAPABILITY_NAMES if c not in html)
    assert missing == [], f"capabilities.html missing caps: {missing}"


def test_admin_api_documents_every_provider_kind() -> None:
    # <code>-wrapped, not bare substring: "meta" would otherwise be satisfied
    # by the word "metadata" anywhere on the page.
    html = (DOCS / "admin-api.html").read_text(encoding="utf-8")
    missing = sorted(k for k in AGENTCLI_PROVIDERS if f"<code>{k}</code>" not in html)
    assert missing == [], f"admin-api.html provider kind enum missing: {missing}"


def test_admin_api_documents_every_emitted_error_code() -> None:
    src = (PKG / "admin_view.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'_err\(\s*"([a-z_]+)"', src))
    assert len(codes) >= 6, f"error-code extraction broke: {sorted(codes)}"
    html = (DOCS / "admin-api.html").read_text(encoding="utf-8")
    missing = sorted(c for c in codes if f"<code>{c}</code>" not in html)
    assert missing == [], f"admin-api.html error-code table missing: {missing}"


def test_admin_api_documents_every_sse_frame() -> None:
    src = (PKG / "agentcli.py").read_text(encoding="utf-8")
    frames = set(re.findall(r'emit\(\s*"([a-z_]+)"', src))
    assert len(frames) >= 10, f"SSE frame extraction broke: {sorted(frames)}"
    html = (DOCS / "admin-api.html").read_text(encoding="utf-8")
    missing = sorted(f for f in frames if f"<code>{f}</code>" not in html)
    assert missing == [], f"admin-api.html SSE frame list missing: {missing}"


def test_operations_route_table_lists_every_token_facing_route() -> None:
    from homeassistant.components.http import HomeAssistantView

    from custom_components.phoenix_mcp import mcp_view, proxy_view

    urls: set[str] = set()
    for module in (proxy_view, mcp_view):
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, HomeAssistantView):
                url = obj.__dict__.get("url")
                if isinstance(url, str):
                    urls.add(url)
    assert len(urls) >= 10, f"route extraction broke: {sorted(urls)}"
    html = (DOCS / "operations.html").read_text(encoding="utf-8")
    missing = sorted(u for u in urls if u not in html)
    assert missing == [], f"operations.html route table missing: {missing}"


@pytest.mark.parametrize(
    ("feature", "floor"),
    [("Assist", ASSIST_API_MIN_HA), ("AI Task", AI_TASK_MIN_HA)],
)
def test_connect_page_states_the_ha_version_floor(feature: str, floor: str) -> None:
    """The optional AI surfaces need newer HA than the headline 2024.5 floor."""
    major_minor = ".".join(floor.split(".")[:2])
    html = (DOCS / "connect.html").read_text(encoding="utf-8")
    assert major_minor in html, (
        f"connect.html never mentions that {feature} requires HA {major_minor}+"
    )
