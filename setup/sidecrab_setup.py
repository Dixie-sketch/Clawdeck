"""SideCrab setup for macOS: settings.json, config.json and the LaunchAgents.

One module, stdlib only, importable. Structured the way ``SideCrab.Common.ps1`` is:
pure decision helpers first, then the thin impure wrappers that carry them out, then
the commands. Every impure dependency - launchctl, security, lsof, HTTP, the clock,
the interpreter probe - reaches this module through :class:`Environment`, so the whole
suite runs headless against a temporary HOME with nothing installed.

    setup/install.sh [--with-toast] [--with-approvals|--no-approvals] [--force-enable] [--yes]
    setup/install.sh --status | --doctor | --pairing-code | --limits-token
    setup/update.sh
    setup/uninstall.sh [--purge] [--yes]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------ constants

#: The floor. 3.13 is what the project targets; below it the LaunchAgent would run an
#: interpreter this code has never been exercised on.
PYTHON_MIN = (3, 13)

#: How an operator fixes a failed interpreter search. Named in the rejection message
#: because the usual cause is Apple's /usr/bin/python3 stub, which looks installed.
PYTHON_HELP = (
    "install Python 3.13 or newer (brew install python@3.13, or python.org) - the "
    "LaunchAgent stores an absolute interpreter path, so it must be a real install"
)

#: Newest name first: a machine with both 3.14 and an older default python3 should get
#: the one that was installed on purpose, not whatever ``python3`` happens to point at.
PYTHON_NAMES = ("python3.14", "python3.13", "python3")

#: Searched after PATH, because a LaunchAgent's PATH is not the operator's login PATH -
#: the two Homebrew prefixes are where a brew-installed interpreter actually lives.
PYTHON_EXTRA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


# ------------------------------------------------------------------ pure: interpreter


@dataclass(frozen=True)
class PythonChoice:
    """The interpreter the plists will name, plus every candidate that was refused."""

    path: str | None
    version: tuple[int, int] | None
    rejected: tuple[tuple[str, str], ...] = ()


def parse_probe_version(out: str) -> tuple[int, int] | None:
    """``3.13`` (what the probe prints) as a tuple, or None when it printed anything else."""
    match = re.search(r"\b(\d+)\.(\d+)\b", out or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def python_candidates(override, path_dirs, is_file) -> list[str]:
    """Absolute interpreter paths to probe, best first, no duplicates. Pure.

    ``$SIDECRAB_PYTHON`` wins outright: an operator naming an interpreter has already
    made the decision this search exists to make.
    """
    out: list[str] = []

    def add(path: str) -> None:
        if path and path not in out and is_file(path):
            out.append(path)

    if override:
        add(override)
    for name in PYTHON_NAMES:
        for directory in list(path_dirs) + list(PYTHON_EXTRA_DIRS):
            add(f"{directory.rstrip('/')}/{name}")
    return out


def python_failure_message(choice: PythonChoice) -> str:
    """Why no interpreter was accepted, and what to do about it."""
    lines = [f"No usable Python found. SideCrab needs {PYTHON_MIN[0]}.{PYTHON_MIN[1]} or newer."]
    for path, reason in choice.rejected:
        lines.append(f"  refused {path}: {reason}")
    lines.append(f"  Fix: {PYTHON_HELP}.")
    lines.append("  Or point SIDECRAB_PYTHON at the interpreter you want.")
    return "\n".join(lines)


def choose_python(candidates, probe) -> PythonChoice:
    """The first candidate whose probe prints PYTHON_MIN or later. Pure.

    ``probe(path)`` returns ``(code, out, err)``. A candidate that exits non-zero, prints
    nothing parseable, or reports an older version is refused with its reason recorded -
    Apple's /usr/bin/python3 command-line-tools stub is exactly the second case, and a
    silent skip would leave the operator staring at "no python found" with a python3 on
    their PATH.
    """
    rejected: list[tuple[str, str]] = []
    for path in candidates:
        code, out, err = probe(path)
        if code != 0:
            detail = (err or out or "").strip().splitlines()
            rejected.append((path, f"exited {code}" + (f": {detail[0]}" if detail else "")))
            continue
        version = parse_probe_version(out)
        if version is None:
            rejected.append((path, "printed no version"))
            continue
        if version < PYTHON_MIN:
            rejected.append((path, "%d.%d is older than %d.%d" % (version + PYTHON_MIN)))
            continue
        return PythonChoice(path=path, version=version, rejected=tuple(rejected))
    return PythonChoice(path=None, version=None, rejected=tuple(rejected))
