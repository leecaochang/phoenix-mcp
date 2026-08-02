"""Record which English each locale was translated from, and report the drift.

Every other i18n guard judges a locale against en.json AS IT IS NOW: the key is
present, the placeholders match, the value is not the English one. A translation
made from English that has since been REWORDED passes all of them. It is
complete, its placeholders still line up, and it is still not identical to the
current English. Nothing reports it, so the locale keeps confidently stating
whatever the string used to say.

So this stamps a fingerprint of the English behind each translated key, into
tests/contract/i18n_source_hashes.json. When the English moves, the stamp no
longer matches and tests/test_i18n_source_drift.py names the keys.

Re-stamping without retranslating is legal and sometimes right (fixing an
English typo does not invalidate a translation), but it is then a visible line
in the diff rather than silence. Only the fingerprint is stored; the English it
was taken from is in en.json's own git history.

scripts/i18n_merge_locale.py stamps what it merges, so a locale written the
normal way needs nothing here.

Run: python scripts/i18n_source_stamp.py zh-Hans
     python scripts/i18n_source_stamp.py --all
     python scripts/i18n_source_stamp.py --check zh-Hans
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "contract" / "i18n_source_hashes.json"

COMMENT = (
    "Fingerprint of the en.json value each locale key was translated from. "
    "Written by scripts/i18n_source_stamp.py, read by tests/test_i18n_source_drift.py. "
    "A mismatch means the English was reworded and that locale now describes the old behaviour."
)


def _load_checker():
    """The sibling checker, by path: scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(
        "i18n_check_locale", pathlib.Path(__file__).with_name("i18n_check_locale.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def digest(value: str) -> str:
    """Short, stable fingerprint of one English string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def locales() -> list[str]:
    """Every shipped locale. en.json is the source, strings.json is not a locale."""
    return checker.languages()


def catalog(name: str) -> dict[str, str]:
    """One language's whole catalog, flattened.

    Through the checker, so this reads BOTH halves of the split catalog (see
    i18n_check_locale.SECTION_DIRS). Reading only one would leave the other's
    keys stamped-but-absent, which this script reports as drift.
    """
    return checker.flatten(checker.load(name))


def load_baseline() -> dict[str, dict[str, str]]:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8")).get("locales", {})


def audit(
    english: dict[str, str], locale: dict[str, str], stamped: dict[str, str]
) -> dict[str, list[str]]:
    """Classify a locale against its stamps. Pure, so the test can prove it bites.

    stale: the English moved since this key was translated.
    unstamped: translated but never fingerprinted (nothing would notice drift).
    orphaned: a stamp for a key the locale or en.json no longer has.
    """
    return {
        "stale": sorted(
            k for k, v in locale.items()
            if k in english and k in stamped and stamped[k] != digest(english[k])
        ),
        "unstamped": sorted(k for k in locale if k in english and k not in stamped),
        "orphaned": sorted(k for k in stamped if k not in locale or k not in english),
    }


def check(language: str) -> int:
    """Report drift for one locale. 0 when the stamps and the English agree."""
    english = catalog("en")
    problems = audit(english, catalog(language), load_baseline().get(language, {}))
    if not any(problems.values()):
        return 0

    if problems["stale"]:
        print("English changed since these were translated; retranslate or re-stamp:")
        for key in problems["stale"]:
            print(f"  {key}\n    english now: {english[key]!r}")
    if problems["unstamped"]:
        print("translated but never stamped; nothing would report drift on them:")
        for key in problems["unstamped"]:
            print(f"  {key}")
    if problems["orphaned"]:
        print("stamped but no longer translated (or gone from en.json):")
        for key in problems["orphaned"]:
            print(f"  {key}")
    print(f"\nRun: python scripts/i18n_source_stamp.py {language}")
    return 1


def stamp(language: str) -> int:
    """Fingerprint every key this locale currently translates."""
    english = catalog("en")
    stamps = {k: digest(english[k]) for k in sorted(catalog(language)) if k in english}
    baseline = load_baseline()
    baseline[language] = stamps
    BASELINE.write_text(
        json.dumps({"_comment": COMMENT, "locales": dict(sorted(baseline.items()))}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"stamped {len(stamps)} keys for {language}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    checking = "--check" in args
    names = [a for a in args if not a.startswith("--")]
    targets = locales() if "--all" in args else names
    if not targets:
        print(__doc__)
        return 2
    return max(check(name) if checking else stamp(name) for name in targets)


if __name__ == "__main__":
    sys.exit(main())
