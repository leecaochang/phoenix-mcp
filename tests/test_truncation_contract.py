"""A read that returns fewer rows than matched has to say so.

An agent cannot ask a follow-up question about data it does not know was
withheld. A clipped page that looks complete is worse than an error, because the
agent proceeds confidently on a partial world: fifty errors out of fifty reads as
a bounded problem, fifty out of five hundred does not, and before this contract
`get_logs` returned the two identically. That tool is the one an agent reaches
for to answer "is anything wrong with this instance", so it was the worst place
in the surface to be silent.

Two kinds of test here. The behavioural ones pin the actual field values for each
paginating read, on both the MCP and REST surfaces, because a caller must not
have to know which surface it used to learn whether it saw everything. The AST
guard covers the tools nobody has written yet: it finds every limit-slice in the
package and requires the enclosing function to report either `truncated` or a
pre-slice `total`, so a new paginating read cannot ship silent.
"""

from __future__ import annotations

import ast
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from custom_components.phoenix_mcp.const import DOMAIN
from custom_components.phoenix_mcp.helpers import collect_log_entries
from custom_components.phoenix_mcp.mcp_view import _dispatch_mcp
from tests.test_mcp_view import _make_data, _make_hass, _make_token

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "phoenix_mcp"

_SKIP_DIRS = {"mesa_core"}

# Either satisfies the contract: a boolean the caller reports directly, or a
# pre-slice count a caller can compare against what it returned. collect_log_entries
# is the second kind, which is why "truncated" alone would be the wrong test.
_SIGNALS = ("truncated", "total")

# Functions whose limit-slice bounds the LENGTH OF ONE VALUE rather than the size
# of a result set. Nothing is withheld that a caller could have asked for, so
# there is no follow-up question a truncation flag would enable. Each entry needs
# a written reason; the point of the list is that adding one is a decision.
_NOT_PAGINATION = {
    # Clips an over-long card name/description harvested from third-party card
    # code to a bound. The value is a string, not a page of rows.
    "card_catalog.py:_clean_str",
}


def _log_record(name: str = "homeassistant.components.light") -> MagicMock:
    record = MagicMock()
    record.level = "ERROR"
    record.name = name
    record.message = ["it broke"]
    record.source = ("light/__init__.py", 1)
    record.timestamp = 0
    record.exception = ""
    record.count = 1
    return record


def _hass_with_logs(data, count: int):
    hass = _make_hass(data)
    syslog = MagicMock()
    syslog.records = {f"k{i}": _log_record() for i in range(count)}
    hass.data = {DOMAIN: data, "system_log": syslog}
    return hass


async def _call_get_logs(hass, token, data, args: dict) -> dict:
    res, _m, _r, outcome = await _dispatch_mcp(
        "tools/call", 3, {"name": "get_logs", "arguments": args},
        token, hass, data, "127.0.0.1", base_url="http://h",
    )
    assert outcome == "allowed"
    return json.loads(res["result"]["content"][0]["text"])


class TestGetLogsReportsTruncation:
    @pytest.mark.asyncio
    async def test_clipped_page_is_flagged_and_counts_the_rest(self):
        token, _ = _make_token(cap_log_read="allow")
        data = _make_data(token)
        hass = _hass_with_logs(data, 10)
        body = await _call_get_logs(hass, token, data, {"limit": 3})
        assert body["count"] == 3
        assert body["total"] == 10
        assert body["truncated"] is True

    @pytest.mark.asyncio
    async def test_complete_page_is_not_flagged(self):
        token, _ = _make_token(cap_log_read="allow")
        data = _make_data(token)
        hass = _hass_with_logs(data, 3)
        body = await _call_get_logs(hass, token, data, {"limit": 10})
        assert body["count"] == 3
        assert body["total"] == 3
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_total_counts_after_filtering_not_before(self):
        """`total` answers "how many matched", not "how many exist", or an agent
        that narrowed by integration would read the instance-wide count as its
        own result set and conclude it was still missing rows."""
        token, _ = _make_token(cap_log_read="allow")
        data = _make_data(token)
        hass = _make_hass(data)
        syslog = MagicMock()
        syslog.records = {
            "a": _log_record("homeassistant.components.light"),
            "b": _log_record("homeassistant.components.light"),
            "c": _log_record("homeassistant.components.climate"),
        }
        hass.data = {DOMAIN: data, "system_log": syslog}
        body = await _call_get_logs(hass, token, data, {"integration": "light"})
        assert body["total"] == 2
        assert body["truncated"] is False

    def test_helper_reports_the_pre_slice_total(self):
        """The pair exists so a caller cannot report a clipped page as the whole
        log by accident; unpacking is forced at every call site."""
        data = MagicMock()
        hass = MagicMock()
        syslog = MagicMock()
        syslog.records = {f"k{i}": _log_record() for i in range(7)}
        hass.data = {DOMAIN: data, "system_log": syslog}
        page = collect_log_entries(hass, "WARNING", None, 2)
        assert len(page.entries) == 2
        assert page.total == 7


class TestRestLogsMatchesTheMcpTool:
    """Surface parity: the same question answered the same way on both."""

    @pytest.mark.asyncio
    async def test_rest_reports_the_same_three_fields(self):
        from custom_components.phoenix_mcp.helpers import collect_log_entries as rest_collect
        data = MagicMock()
        hass = MagicMock()
        syslog = MagicMock()
        syslog.records = {f"k{i}": _log_record() for i in range(10)}
        hass.data = {DOMAIN: data, "system_log": syslog}
        page = rest_collect(hass, "WARNING", None, 4)
        # The REST view builds its body from exactly these, so pinning the pair
        # pins both surfaces without standing up an aiohttp request here; the
        # AST guard below is what keeps the view from drifting away from it.
        body = {
            "count": len(page.entries),
            "total": page.total,
            "truncated": page.total > len(page.entries),
        }
        assert body == {"count": 4, "total": 10, "truncated": True}

    def test_rest_view_builds_all_three_fields(self):
        """Read the view's own source, so deleting a field there fails here."""
        source = (PACKAGE / "proxy_view.py").read_text(encoding="utf-8")
        marker = source.index('"The system_log integration is not loaded')
        tail = source[marker:marker + 1200]
        for field in ("count", "total", "truncated", "entries"):
            assert f'"{field}"' in tail, f"REST logs response dropped {field}"


class TestLimitSlicesReportTruncation:
    """Every limit-slice in the package must be reportable by its caller.

    The behavioural tests above cover the reads that exist today. This covers the
    ones nobody has written: a new paginating read that slices by a limit and says
    nothing fails here rather than shipping silent, which is exactly how get_logs
    stayed silent through several passes over this surface.
    """

    @staticmethod
    def _slice_bound_name(node: ast.Subscript) -> str | None:
        """The name of a slice's upper bound, when it is a name at all.

        `entries[:limit]` and `matches[:f.limit]` are the paginating shape.
        `hex[:16]` and `parts[:2]` are constants doing unrelated string work and
        are not pagination, so an unnamed bound is not a finding.
        """
        if not isinstance(node.slice, ast.Slice) or node.slice.upper is None:
            return None
        upper = node.slice.upper
        if isinstance(upper, ast.Name):
            return upper.id
        if isinstance(upper, ast.Attribute):
            return upper.attr
        return None

    @classmethod
    def _findings(cls) -> list[str]:
        findings = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in path.relative_to(PACKAGE).parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                slices = [
                    node for node in ast.walk(func)
                    if isinstance(node, ast.Subscript)
                    and (name := cls._slice_bound_name(node)) is not None
                    and "limit" in name.lower()
                ]
                if not slices:
                    continue
                if f"{path.relative_to(PACKAGE)}:{func.name}" in _NOT_PAGINATION:
                    continue
                # A signal can be a response-body key ("truncated": ...) or the
                # keyword of a returned pair (LogEntryPage(total=...)); both tell
                # the caller the same thing, so both count.
                reported = any(
                    (
                        isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node.value in _SIGNALS
                    ) or (
                        isinstance(node, ast.keyword) and node.arg in _SIGNALS
                    )
                    for node in ast.walk(func)
                )
                if not reported:
                    findings.append(
                        f"{path.relative_to(PACKAGE)}:{slices[0].lineno} ({func.name})"
                    )
        return findings

    def test_every_limit_slice_reports_truncation_or_a_total(self):
        assert not self._findings(), (
            "These functions clip a result set by a limit without reporting "
            '"truncated" or a pre-slice "total", so a caller cannot tell a '
            "complete answer from a clipped one: " + ", ".join(self._findings())
        )

    def test_the_walk_actually_finds_limit_slices(self):
        """The test above asserts an ABSENCE, so prove the walk is not blind.

        A detector that silently matched nothing would pass forever while the
        defect it exists to catch walked straight back in.
        """
        tree = ast.parse("x = rows[:limit]\ny = rows[:f.limit]\nz = h[:16]\n")
        subscripts = [n for n in ast.walk(tree) if isinstance(n, ast.Subscript)]
        bounds = [self._slice_bound_name(n) for n in subscripts]
        assert "limit" in bounds, "a bare [:limit] must be detected"
        assert bounds.count("limit") == 2, "an attribute bound like f.limit must be detected"
        assert None in bounds, "a constant bound like [:16] must not be treated as pagination"

    def test_no_stale_exemptions(self):
        """An exemption whose function no longer exists reads as a considered
        decision while covering nothing, so it must be removed rather than kept
        as harmless clutter."""
        for entry in _NOT_PAGINATION:
            filename, func_name = entry.split(":")
            tree = ast.parse((PACKAGE / filename).read_text(encoding="utf-8"))
            names = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert func_name in names, f"exempted function {entry} no longer exists"

    def test_the_walk_covers_the_module_that_regressed(self):
        """helpers.py holds the slice that made get_logs silent, so a path-filter
        change cannot quietly drop it from the scan."""
        scanned = {
            str(p.relative_to(PACKAGE)) for p in PACKAGE.rglob("*.py")
            if not any(part in _SKIP_DIRS for part in p.relative_to(PACKAGE).parts)
        }
        assert {"helpers.py", "mcp_view.py", "proxy_view.py"} <= scanned
