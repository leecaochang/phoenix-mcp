"""Generate the generated catalog sections from their Python templates.

An approval's summary is produced by Python (it is persisted on the record as
the audit trail) and rendered by the panel (which must show it in the operator's
language). Both have to be the same sentence, so only one of them gets to author
it: const.DIFF_SUMMARY_TEMPLATES. This copies that dict into the `diff` section
of translations/en.json.

Run after adding or changing a template. A contract test fails if the two ever
disagree, so this is not something to keep in sync by hand.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from custom_components.phoenix_mcp.const import (  # noqa: E402
    DIFF_SUMMARY_TEMPLATES,
    MESA_SUGGESTION_PHRASES,
    MESA_SUGGESTION_TEMPLATES,
    NOTIFICATION_TEMPLATES,
    VOICE_TEMPLATES,
    VERSION_SUMMARY_TEMPLATES,
)

CATALOG = REPO / "custom_components" / "phoenix_mcp" / "catalogs" / "en.json"


def main() -> None:
    # Written FLAT: a variant key like "blueprint.edit.consumers" sits beside the
    # base "blueprint.edit", which is a string, so the two cannot both exist in a
    # nested object. HA's loader and the panel both flatten by joining with dots,
    # so a literal dotted key resolves to exactly the same lookup path.
    doc = json.loads(CATALOG.read_text())
    doc["panel"]["diff"] = dict(sorted(DIFF_SUMMARY_TEMPLATES.items()))
    doc["panel"]["version"] = dict(sorted(VERSION_SUMMARY_TEMPLATES.items()))
    # MESA suggestion reasons: the sentence templates plus the sub-phrases they
    # interpolate, which need their own entries or a translated sentence would
    # still splice an English clause into itself.
    doc["panel"]["mesaSuggestion"] = dict(sorted(
        {**MESA_SUGGESTION_TEMPLATES, **MESA_SUGGESTION_PHRASES}.items()
    ))
    # Notifications are HA-rendered, not panel-rendered, so they sit in their
    # own top-level section that the panel never fetches.
    doc["notification"] = dict(sorted(NOTIFICATION_TEMPLATES.items()))
    # Spoken voice-agent declines: HA-rendered like notifications, but resolved
    # against the CONVERSATION's language rather than the server's.
    doc["voice"] = dict(sorted(VOICE_TEMPLATES.items()))
    CATALOG.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(DIFF_SUMMARY_TEMPLATES)} diff, "
          f"{len(VERSION_SUMMARY_TEMPLATES)} version, "
          f"{len(MESA_SUGGESTION_TEMPLATES) + len(MESA_SUGGESTION_PHRASES)} mesaSuggestion, "
          f"{len(NOTIFICATION_TEMPLATES)} notification and "
          f"{len(VOICE_TEMPLATES)} voice templates into {CATALOG.name}")


if __name__ == "__main__":
    main()
