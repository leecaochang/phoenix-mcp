"""Security policy checks for GitHub Actions workflow references."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _uses(path: Path, job: str) -> list[str]:
    """Return action refs from one top-level job without a YAML dependency."""
    lines = path.read_text(encoding="utf-8").splitlines()
    in_jobs = False
    in_target = False
    refs: list[str] = []
    for line in lines:
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        match = re.match(r"^  ([a-zA-Z0-9_-]+):$", line)
        if match:
            in_target = match.group(1) == job
            continue
        if in_target:
            uses = re.match(r"^\s+-?\s*uses:\s*([^\s#]+)", line)
            if uses:
                refs.append(uses.group(1))
    return refs


def _jobs(path: Path) -> list[str]:
    """Return only top-level keys below the jobs mapping."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index("jobs:") + 1
    return [
        match.group(1)
        for line in lines[start:]
        if (match := re.match(r"^  ([a-zA-Z0-9_-]+):$", line))
    ]


def test_merge_blocking_workflow_actions_are_pinned() -> None:
    for filename in ("tests.yml", "hacs.yml", "hassfest.yml"):
        path = WORKFLOWS / filename
        for job in _jobs(path):
            if job == "compatibility-canary":
                continue
            refs = _uses(path, job)
            assert refs, f"{filename}:{job} has no action references"
            assert all(SHA_REF.fullmatch(ref) for ref in refs), (filename, job, refs)


def test_mutable_refs_are_isolated_to_scheduled_canaries() -> None:
    expected = {
        "hacs.yml": "hacs/action@main",
        "hassfest.yml": "home-assistant/actions/hassfest@master",
    }
    for filename, mutable_ref in expected.items():
        path = WORKFLOWS / filename
        text = path.read_text(encoding="utf-8")
        assert "if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" in text
        assert _uses(path, "compatibility-canary").count(mutable_ref) == 1
        assert text.count(mutable_ref) == 1
