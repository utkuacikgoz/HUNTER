#!/usr/bin/env python3
"""Fail if requirements.lock has drifted from the direct pins in requirements.txt.

Runtime and CI install from `requirements.lock` (the fully-pinned freeze with exact
transitives), while `requirements.txt` holds the direct dependencies. Dependabot's
pip config only bumps requirements.txt, so a bump can land there while the lock — the
file we actually install — keeps the old version. That drift is silent: tests pass
against the stale lock. This guard makes it loud.

For every `name==version` in requirements.txt it asserts the lock pins the SAME
version. It does not police transitive deps (those live only in the lock); it catches
exactly the "direct pin bumped in one file but not the other" case.

To regenerate the lock after changing requirements.txt:
    pip install -r requirements.txt && pip freeze > requirements.lock
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name[extra]==version ; marker  →  capture name and version (extras/markers ignored).
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^;\s]+)")


def _normalize(name: str) -> str:
    """PEP 503 normalization so e.g. `python_dotenv` and `python-dotenv` match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # skip blanks/comments and options like `-r`
            continue
        m = _PIN.match(line)
        if m:
            pins[_normalize(m.group(1))] = m.group(2)
    return pins


def main() -> int:
    direct = parse_pins(ROOT / "requirements.txt")
    lock = parse_pins(ROOT / "requirements.lock")

    drift = [(name, ver, lock.get(name)) for name, ver in sorted(direct.items()) if lock.get(name) != ver]
    if drift:
        print("requirements.txt and requirements.lock disagree on direct pins:\n")
        for name, want, have in drift:
            print(f"  {name}: requirements.txt=={want} but requirements.lock=={have or 'MISSING'}")
        print(
            "\nThe lock is what CI and prod install. Regenerate it after bumping requirements.txt:\n"
            "    pip install -r requirements.txt && pip freeze > requirements.lock"
        )
        return 1

    print(f"OK: all {len(direct)} direct pins in requirements.txt match requirements.lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
