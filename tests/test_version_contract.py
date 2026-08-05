"""The release version is stated in four files and nothing kept them equal.

`const.PHOENIX_VERSION` is what an operator and an MCP client both see: it is the
`serverInfo.version` of every `initialize` handshake and the `version` field of
the admin info endpoint. `manifest.json` is what Home Assistant and HACS read to
decide whether an update is available. `package.json` and its lock file version
the panel bundle built from the same tree.

Nothing tied them together, so the first bump that touched one and forgot
another would ship an integration claiming one version to HACS and a different
one to every connected client, which is the kind of drift nobody notices until
someone is debugging a version-specific report. That is the same failure the
Home Assistant floor had before `test_ha_version_floor.py`, in a second place.

The lock file is npm-generated and follows `package.json` on the next install,
so it is checked rather than trusted: a hand-edited bump that skips `npm install`
leaves it behind, and the committed file is what a clean checkout builds from.
"""

from __future__ import annotations

import json
import pathlib
import re

from custom_components.phoenix_mcp.const import PHOENIX_VERSION

REPO = pathlib.Path(__file__).resolve().parent.parent

# Major.minor.patch, no pre-release or build metadata. HACS compares versions to
# decide what to offer as an update, and Home Assistant renders the manifest
# value verbatim, so an exotic form is a compatibility question rather than a
# style one. Widen this deliberately if a pre-release is ever wanted.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_is_a_plain_semver_string():
    assert SEMVER.match(PHOENIX_VERSION), (
        f"PHOENIX_VERSION is {PHOENIX_VERSION!r}; HACS compares this to decide "
        "whether an update is available, so it must be major.minor.patch."
    )


def test_the_manifest_states_the_same_version():
    manifest = json.loads(
        (REPO / "custom_components" / "phoenix_mcp" / "manifest.json").read_text())
    assert manifest["version"] == PHOENIX_VERSION, (
        f"manifest.json says {manifest['version']!r} and const.PHOENIX_VERSION says "
        f"{PHOENIX_VERSION!r}. HACS reads the manifest; every MCP client reads the "
        "constant. They must not disagree."
    )


def test_the_panel_package_states_the_same_version():
    package = json.loads((REPO / "package.json").read_text())
    assert package["version"] == PHOENIX_VERSION, (
        f"package.json says {package['version']!r} and const.PHOENIX_VERSION says "
        f"{PHOENIX_VERSION!r}. The panel bundle is built from this tree and is "
        "released with it."
    )


def test_the_lock_file_followed_the_package():
    """npm rewrites this on install; a hand bump that skipped it leaves it stale."""
    lock = json.loads((REPO / "package-lock.json").read_text())
    root = lock.get("packages", {}).get("", {})
    assert lock["version"] == PHOENIX_VERSION, (
        f"package-lock.json root version is {lock['version']!r}, expected "
        f"{PHOENIX_VERSION!r}. Run npm install after bumping package.json."
    )
    assert root.get("version") == PHOENIX_VERSION, (
        f"package-lock.json packages.\"\" version is {root.get('version')!r}, "
        f"expected {PHOENIX_VERSION!r}. Run npm install after bumping package.json."
    )
