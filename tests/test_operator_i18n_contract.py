"""Contracts that keep Phoenix-authored operator text behind localization keys."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
BACKEND_FILES = (
    ROOT / "custom_components/phoenix_mcp/admin_view.py",
    ROOT / "custom_components/phoenix_mcp/agentcli.py",
)
CATALOG_FILES = (
    ROOT / "custom_components/phoenix_mcp/catalogs/en.json",
    ROOT / "custom_components/phoenix_mcp/catalogs/zh-Hans.json",
)


def _panel_catalog(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["panel"]


def _literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_string_choices(node: ast.AST | None) -> list[str]:
    literal = _literal_string(node)
    if literal is not None:
        return [literal]
    if isinstance(node, ast.IfExp):
        choices = [*_literal_string_choices(node.body), *_literal_string_choices(node.orelse)]
        return choices if len(choices) == 2 else []
    return []


def _dict_items(node: ast.Dict) -> dict[str, ast.AST]:
    return {
        key.value: value
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_every_admin_error_is_keyed_or_explicit_upstream_passthrough() -> None:
    catalogs = [_panel_catalog(path)["adminError"] for path in CATALOG_FILES]
    findings: list[str] = []

    for path in BACKEND_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_err"
            ):
                continue
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            has_key = "key" in kwargs
            has_passthrough = "passthrough" in kwargs
            if has_key == has_passthrough:
                findings.append(
                    f"{path.name}:{call.lineno} must declare exactly one of key or passthrough"
                )
                continue
            if has_passthrough:
                value = kwargs["passthrough"]
                if not (isinstance(value, ast.Constant) and value.value is True):
                    findings.append(f"{path.name}:{call.lineno} passthrough must be literal True")
                message = call.args[1] if len(call.args) > 1 else kwargs.get("message")
                if isinstance(message, (ast.Constant, ast.JoinedStr)):
                    findings.append(
                        f"{path.name}:{call.lineno} Phoenix-authored literals cannot be passthrough"
                    )
                continue

            key = _literal_string(kwargs["key"])
            # The only dynamic key is a ProbeConfigError instance. Its
            # constructors are checked separately below.
            if key is None:
                if not (
                    isinstance(kwargs["key"], ast.Attribute)
                    and isinstance(kwargs["key"].value, ast.Name)
                    and kwargs["key"].value.id == "probe"
                    and kwargs["key"].attr == "key"
                ):
                    findings.append(f"{path.name}:{call.lineno} has an uncheckable dynamic key")
                continue
            for catalog_path, catalog in zip(CATALOG_FILES, catalogs):
                if key not in catalog:
                    findings.append(f"{path.name}:{call.lineno} adminError.{key} missing from {catalog_path.name}")

    assert findings == []


def test_probe_config_errors_have_catalog_keys() -> None:
    path = ROOT / "custom_components/phoenix_mcp/agentcli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    catalogs = [_panel_catalog(catalog_path)["adminError"] for catalog_path in CATALOG_FILES]
    keys: list[tuple[int, str]] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "ProbeConfigError"
        ):
            continue
        key = _literal_string(call.args[1] if len(call.args) > 1 else None)
        assert key is not None, f"agentcli.py:{call.lineno} ProbeConfigError key must be literal"
        keys.append((call.lineno, key))

    assert keys, "ProbeConfigError constructors were not found"
    for line, key in keys:
        for catalog_path, catalog in zip(CATALOG_FILES, catalogs):
            assert key in catalog, f"agentcli.py:{line} adminError.{key} missing from {catalog_path.name}"


def test_successful_error_payloads_declare_key_or_passthrough() -> None:
    """HTTP 200 probe failures bypass _err, so guard their payloads too."""
    path = ROOT / "custom_components/phoenix_mcp/agentcli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    findings: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        items = _dict_items(node)
        ok = items.get("ok")
        if not (isinstance(ok, ast.Constant) and ok.value is False):
            continue
        if not ({"message_key", "message_passthrough"} & items.keys()):
            findings.append(node.lineno)
    assert findings == []


def test_agent_chat_authored_sse_codes_and_progress_have_catalog_keys() -> None:
    catalogs = [_panel_catalog(path) for path in CATALOG_FILES]
    findings: list[str] = []

    agent_path = ROOT / "custom_components/phoenix_mcp/agentcli.py"
    agent_tree = ast.parse(agent_path.read_text(encoding="utf-8"))
    for call in ast.walk(agent_tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "emit"
            and len(call.args) >= 2
            and _literal_string(call.args[0]) in {"notice", "error"}
            and isinstance(call.args[1], ast.Dict)
        ):
            continue
        code = _literal_string(_dict_items(call.args[1]).get("code"))
        if code is None:
            continue
        for catalog_path, catalog in zip(CATALOG_FILES, catalogs):
            if code not in catalog["agentchat"]["notice"]:
                findings.append(f"agentcli.py:{call.lineno} agentchat.notice.{code} missing from {catalog_path.name}")

    for relative in (
        "custom_components/phoenix_mcp/tool_common.py",
        "custom_components/phoenix_mcp/mcp_view.py",
        "custom_components/phoenix_mcp/tools/esphome.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_set_progress_status"
                and call.args
                and not (isinstance(call.args[0], ast.Constant) and call.args[0].value is None)
            ):
                continue
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            keys = _literal_string_choices(kwargs.get("key"))
            if not keys:
                findings.append(f"{relative}:{call.lineno} progress text has no literal key")
                continue
            for key in keys:
                parts = key.split(".")
                for catalog_path, catalog in zip(CATALOG_FILES, catalogs):
                    value: object = catalog
                    for part in parts:
                        value = value.get(part) if isinstance(value, dict) else None
                    if not isinstance(value, str):
                        findings.append(f"{relative}:{call.lineno} {key} missing from {catalog_path.name}")

    assert findings == []


def test_tool_image_sse_contains_data_not_backend_authored_alt_text() -> None:
    source = (ROOT / "custom_components/phoenix_mcp/agentcli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    payloads: list[ast.Dict] = []
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "emit"
            and len(call.args) >= 2
            and _literal_string(call.args[0]) == "tool_image"
            and isinstance(call.args[1], ast.Dict)
        ):
            payloads.append(call.args[1])
    assert payloads
    for payload in payloads:
        assert "alt" not in _dict_items(payload)
