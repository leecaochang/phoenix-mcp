"""Merge a batch of translations into a locale file, mirroring en.json's shape.

Translations are written a section at a time as a flat {dotted key: string}
batch. This places each one at the same position en.json holds it, which is not
a plain dot-split: the generated diff and version sections store literal dotted
keys inside one object, because "blueprint.edit" is both a string and the parent
of "blueprint.edit.consumers" and the two cannot nest together.

Refuses any key en.json does not define, so a typo cannot land as a dead entry
that HA will silently never use.

Run: python scripts/i18n_merge_locale.py zh-Hans batch.json
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_sibling(name: str):
    """A sibling script, by path: scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        name, pathlib.Path(__file__).with_name(f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_sibling("i18n_check_locale")
stamper = _load_sibling("i18n_source_stamp")


def paths(node: object, prefix: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """Map every dotted key in a catalog to the literal path it is stored at."""
    out: dict[str, tuple[str, ...]] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(paths(value, prefix + (key,)))
    elif isinstance(node, str):
        out[".".join(prefix)] = prefix
    return out


def main() -> int:
    language, batch_path = sys.argv[1], sys.argv[2]
    english = checker.load("en")
    layout = paths(english)

    batch: dict[str, str] = json.loads(pathlib.Path(batch_path).read_text(encoding="utf-8"))

    unknown = sorted(k for k in batch if k not in layout)
    if unknown:
        print("not keys in en.json:")
        for key in unknown:
            print(f"  {key}")
        return 1

    nonstring = sorted(k for k, v in batch.items() if not isinstance(v, str))
    if nonstring:
        print("not strings (HA would render these as raw keys):")
        for key in nonstring:
            print(f"  {key}")
        return 1

    keep_english = sorted(k for k in batch if k in checker.KEEP_ENGLISH_KEYS)
    if keep_english:
        print("deliberately stay English; remove them from the batch:")
        for key in keep_english:
            print(f"  {key}")
        return 1

    # Route each key to the file its SECTION lives in: the catalog is split
    # across catalogs/ and translations/ (see i18n_check_locale.SECTION_DIRS), so
    # one batch can legitimately touch both. Writing all of it to one file would
    # put a config string where nothing reads it.
    docs: dict[pathlib.Path, dict] = {}
    for key, value in batch.items():
        path = layout[key]
        target = checker.SECTION_DIRS[path[0]] / f"{language}.json"
        if target not in docs:
            docs[target] = (
                json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            )
        node = docs[target]
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    # Match en.json's own formatting so the two diff cleanly side by side.
    previous = {
        t: (t.read_text(encoding="utf-8") if t.exists() else None) for t in docs
    }
    for target, doc in docs.items():
        target.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # Validate what actually landed rather than the batch in isolation: a
    # placeholder change, a dropped inline tag, or a lost literal only makes
    # sense against the English it replaces, and the checker already knows how
    # to judge all of that. Roll back so a bad batch never leaves a broken file
    # on disk for the next run to build on.
    if checker.check(language) != 0:
        for target, before in previous.items():
            if before is None:
                target.unlink()
            else:
                target.write_text(before, encoding="utf-8")
        names = ", ".join(sorted(t.parent.name + "/" + t.name for t in previous))
        print(f"\nREJECTED: {names} left unchanged.")
        return 1

    # Record the English these translations were made from, so a later rewording
    # of it is reported instead of quietly leaving them describing the old thing.
    stamper.stamp(language)

    names = ", ".join(sorted(t.parent.name + "/" + t.name for t in docs))
    print(f"merged {len(batch)} keys into {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
