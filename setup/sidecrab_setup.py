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

import argparse
import copy
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ constants

#: The port crabd listens on for the macOS/browser build.
PORT = 9999
BASE_URL = f"http://127.0.0.1:{PORT}"

#: The ONE substring that says "SideCrab wrote this settings.json entry". A command hook
#: carries it in `command`, an http hook in `url`, so the concatenation of the two finds
#: both kinds. Install, status, doctor and uninstall all match on this and nothing else -
#: a second copy is how a stale entry from an older port survives an upgrade.
HOOK_MARKER = f"127.0.0.1:{PORT}/v1/hook"

#: What allowedHttpHookUrls must admit for the two http hooks (Stop, PermissionRequest)
#: to be called at all. Both spellings: the allow-list matches the URL as written, and a
#: settings.json that reaches crabd through localhost is as legitimate as one that does
#: not. Arrays merge across settings files, so a project-level list matters too.
ALLOWED_HOOK_PATTERNS = (f"http://127.0.0.1:{PORT}/*", f"http://localhost:{PORT}/*")

#: The substring that identifies OUR statusLine, the way HOOK_MARKER identifies our hooks.
STATUSLINE_MARKER = "sidecrab_statusline"

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


# ------------------------------------------------------------------ pure: settings.json hooks


@dataclass(frozen=True)
class MatcherSplit:
    """One hooks matcher group, split into SideCrab's entries and everybody else's."""

    ours: dict | None
    foreign: dict | None
    our_count: int = 0
    foreign_count: int = 0


def hook_entry_is_ours(entry, marker: str = HOOK_MARKER) -> bool:
    """Does one hook entry belong to SideCrab? Pure.

    A command hook carries the marker in ``command`` and an http hook in ``url``; an
    entry has only one of the two, so concatenating finds both kinds with one marker.
    """
    if not isinstance(entry, dict):
        return False
    return marker in f"{entry.get('command', '')}{entry.get('url', '')}"


def split_hook_matcher(matcher, marker: str = HOOK_MARKER) -> MatcherSplit:
    """Split one matcher group at ENTRY level. Pure.

    NOT at matcher level: the installer writes each hook as its own matcher, so the two
    agree right up until someone hand-merges a hook of their own into one of ours - and
    then a matcher-level decision eats it on the next install or uninstall.

    The unshared cases hand back the ORIGINAL object, so a round trip over an untouched
    settings.json is byte-identical. Only a genuinely shared matcher is rebuilt, and
    then every other key it carries (the matcher pattern, anything a future CLI adds)
    goes onto both halves.
    """
    if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
        # Not a matcher this understands: passed through as foreign rather than dropped.
        return MatcherSplit(ours=None, foreign=matcher, our_count=0, foreign_count=1)

    mine = [h for h in matcher["hooks"] if hook_entry_is_ours(h, marker)]
    theirs = [h for h in matcher["hooks"] if not hook_entry_is_ours(h, marker)]

    if not mine:
        # An empty matcher is nobody's: handed back as foreign so it survives untouched.
        return MatcherSplit(ours=None, foreign=matcher, our_count=0, foreign_count=len(theirs))
    if not theirs:
        return MatcherSplit(ours=matcher, foreign=None, our_count=len(mine), foreign_count=0)

    def rebuild(entries):
        return {k: (entries if k == "hooks" else v) for k, v in matcher.items()}

    return MatcherSplit(
        ours=rebuild(mine),
        foreign=rebuild(theirs),
        our_count=len(mine),
        foreign_count=len(theirs),
    )


def merge_hook_fragment(settings, fragment_hooks, marker: str = HOOK_MARKER):
    """settings.json with the fragment's hooks merged in. Pure - returns a new document.

    Idempotent by construction: every entry of ours is dropped from the event first and
    the fragment's current entries are appended, so a re-run replaces rather than
    duplicates and a stale entry from an older port cannot survive. Foreign entries stay
    where they are, in their own matcher group or sharing one of ours.
    """
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        out["hooks"] = hooks

    merged = 0
    for event, matchers in fragment_hooks.items():
        kept = []
        for matcher in hooks.get(event, []) or []:
            split = split_hook_matcher(matcher, marker)
            if split.foreign is not None:
                kept.append(split.foreign)
        hooks[event] = kept + copy.deepcopy(list(matchers))
        merged += 1
    return out, merged


def remove_hook_entries(settings, marker: str = HOOK_MARKER):
    """settings.json with SideCrab's hook entries removed. Pure - returns a new document.

    Entry level, so a matcher group shared with a hand-merged hook of the operator's
    keeps that hook. A group emptied by the removal goes, and so does an event left with
    no groups - and an empty ``hooks`` object is noise, not configuration.
    """
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, 0

    removed = 0
    for event in list(hooks):
        kept = []
        for matcher in hooks.get(event, []) or []:
            split = split_hook_matcher(matcher, marker)
            removed += split.our_count
            if split.foreign is not None:
                kept.append(split.foreign)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out, removed


def hook_events(settings, marker: str = HOOK_MARKER) -> list[tuple[str, int]]:
    """Which hook events carry a SideCrab entry, and how many each. Pure."""
    hooks = (settings or {}).get("hooks")
    if not isinstance(hooks, dict):
        return []
    out = []
    for event, matchers in hooks.items():
        count = sum(
            1
            for matcher in (matchers or [])
            if isinstance(matcher, dict)
            for entry in (matcher.get("hooks") or [])
            if hook_entry_is_ours(entry, marker)
        )
        if count:
            out.append((event, count))
    return out


# ------------------------------------------------------------------ pure: allowedHttpHookUrls


@dataclass(frozen=True)
class SettingsPlan:
    """A decided change to settings.json: the new document, what happened, and why."""

    settings: dict
    action: str
    reason: str = ""


def hook_url_allowed(url: str, patterns) -> bool:
    """Would Claude Code call this http hook? Pure.

    ``None`` patterns = the key is unset = every URL is allowed (the default). An EMPTY
    list is not the same thing: the operator set the key and admitted nothing.
    """
    if patterns is None:
        return True
    return any(p and fnmatch.fnmatchcase(url, str(p)) for p in patterns)


def allowed_hook_urls_plan(settings) -> SettingsPlan:
    """Ensure the allow-list admits crabd - but ONLY if the operator already set one.

    NEVER CREATE THE KEY. allowedHttpHookUrls is a switch, not a list: absent, every
    http hook is called; present, only URLs matching a pattern are. Writing it to "help"
    would silently block every other http hook the operator has.
    """
    out = copy.deepcopy(settings)
    if "allowedHttpHookUrls" not in out:
        return SettingsPlan(out, "absent", "allowedHttpHookUrls is unset - every hook URL is allowed")

    patterns = list(out.get("allowedHttpHookUrls") or [])
    missing = [p for p in ALLOWED_HOOK_PATTERNS if p not in patterns]
    if not missing:
        return SettingsPlan(out, "present", "allowedHttpHookUrls already admits crabd")
    out["allowedHttpHookUrls"] = patterns + missing
    return SettingsPlan(out, "added", "added " + ", ".join(missing) + " to allowedHttpHookUrls")


def allowed_hook_urls_removal_plan(settings) -> SettingsPlan:
    """Take our patterns back out - unless that would leave the list empty.

    An empty allow-list admits nothing, so dropping our last two patterns out of a list
    that held only them would block every http hook the operator ever adds. The key goes
    instead, which is the state they were in before SideCrab.
    """
    out = copy.deepcopy(settings)
    if "allowedHttpHookUrls" not in out:
        return SettingsPlan(out, "absent", "allowedHttpHookUrls is unset")

    patterns = list(out.get("allowedHttpHookUrls") or [])
    kept = [p for p in patterns if p not in ALLOWED_HOOK_PATTERNS]
    if kept == patterns:
        return SettingsPlan(out, "not-present", "allowedHttpHookUrls holds none of ours")
    if not kept:
        del out["allowedHttpHookUrls"]
        return SettingsPlan(
            out,
            "key-removed",
            "allowedHttpHookUrls held only SideCrab's patterns - the key was removed rather "
            "than left empty, which would have blocked every http hook",
        )
    out["allowedHttpHookUrls"] = kept
    return SettingsPlan(out, "removed", "SideCrab's patterns removed from allowedHttpHookUrls")


# ------------------------------------------------------------------ pure: statusLine


@dataclass(frozen=True)
class Decision:
    """A named decision with the reason an operator gets told."""

    action: str
    changed: bool
    reason: str


def statusline_is_ours(command) -> bool:
    """Does a settings.json statusLine command belong to SideCrab? Pure."""
    return bool(command) and STATUSLINE_MARKER in str(command)


def statusline_command(python: str, script: str) -> str:
    """The exact statusLine command. Pure.

    Quoted only where a path needs it: a repo checked out under a directory with a space
    would otherwise be handed to the shell as two words.
    """
    return f"{shlex.quote(python)} {shlex.quote(script)}"


def statusline_restore_decision(
    current_command, current_is_ours: bool, saved_present: bool, saved_statusline
) -> Decision:
    """What an uninstall should do with settings.json's statusLine. Pure.

    THE RULE, the same one the hook removal follows: never write over a value that is not
    ours. Install SideCrab, then install a different status line B, then uninstall: writing
    the saved prior back unconditionally replaces B with a line SideCrab displaced months
    earlier, and with no prior saved it deletes B outright.

    An ABSENT status line is not a foreign one - there is nothing to preserve, so a saved
    prior goes back into an empty slot.
    """
    has_current = bool(current_command)

    if has_current and not current_is_ours:
        return Decision(
            "preserve-foreign",
            False,
            "the status line configured now is not SideCrab's - it is left exactly as it is",
        )
    if not saved_present:
        if current_is_ours:
            return Decision("remove", True, "ours is installed and no saved prior exists")
        return Decision("none", False, "no SideCrab status line configured")
    if saved_statusline is not None:
        return Decision("restore", True, "the prior status line is put back")
    if has_current:
        return Decision("remove", True, "nothing existed before us - the slot goes back to empty")
    return Decision("none", False, "nothing to restore")


# ------------------------------------------------------------------ the environment seam


class SetupError(Exception):
    """A refusal an operator is meant to read. Never a traceback."""


def _default_run(argv, stdin=None, timeout=None):
    proc = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _default_http(url, body=None, headers=None, timeout=5.0):
    """One request, as ``(status, body)``. A refused connection is (0, reason).

    Never raises for a transport failure: "crabd is not running" is a row on a table,
    not a crashed doctor.
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # refused, DNS, timeout - all the same to the caller
        return 0, str(exc)


def _default_python_probe(path):
    return _default_run([path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=10)


def _is_executable_file(path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


@dataclass
class Environment:
    """Everything this module is not allowed to reach for on its own.

    Constructed once in :func:`main` and threaded through every command, so the suite
    runs against a temporary HOME with no launchctl, no Keychain, no socket and no clock.
    """

    home: Path
    repo_root: Path
    uid: int
    user: str
    now: callable
    run: callable
    http_get: callable
    http_post: callable
    python_probe: callable
    python_override: str | None = None
    is_file: callable = staticmethod(_is_executable_file)
    path_dirs: tuple = ()
    sleep: callable = staticmethod(time.sleep)
    emit: callable = staticmethod(print)
    #: None means "not a TTY": every prompt takes its documented default instead.
    ask: callable | None = None

    @classmethod
    def default(cls, repo_root=None) -> "Environment":
        import getpass

        root = Path(repo_root or Path(__file__).resolve().parent.parent)
        return cls(
            home=Path(os.path.expanduser("~")),
            repo_root=root,
            uid=os.getuid(),
            user=getpass.getuser(),
            now=datetime.now,
            run=_default_run,
            http_get=_default_http,
            http_post=_default_http,
            python_probe=_default_python_probe,
            python_override=os.environ.get("SIDECRAB_PYTHON") or None,
            path_dirs=tuple(p for p in os.environ.get("PATH", "").split(os.pathsep) if p),
            ask=input if sys.stdin.isatty() else None,
        )

    # -- the paths this tool is allowed to touch, and no others

    @property
    def settings_path(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def sidecrab_dir(self) -> Path:
        return self.home / ".sidecrab"

    @property
    def config_path(self) -> Path:
        return self.sidecrab_dir / "config.json"

    @property
    def chain_path(self) -> Path:
        return self.sidecrab_dir / "statusline-chain.json"

    @property
    def token_path(self) -> Path:
        return self.sidecrab_dir / "panel-token"

    @property
    def logs_dir(self) -> Path:
        return self.sidecrab_dir / "logs"

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def resolve_python(self) -> str:
        extra = os.environ.get("SIDECRAB_PYTHON_DIRS")
        dirs = list(self.path_dirs)
        if extra is not None:
            dirs += [d for d in extra.split(os.pathsep) if d]
            candidates = python_candidates(self.python_override, dirs, self.is_file)
        else:
            candidates = python_candidates(self.python_override, dirs, self.is_file)
        choice = choose_python(candidates, self.python_probe)
        if choice.path is None:
            raise SetupError(python_failure_message(choice))
        return choice.path


# ------------------------------------------------------------------ impure: files


class Writer:
    """Every write this tool makes: backed up once per run, then replaced atomically.

    The backup is taken before the FIRST write of a run and named the same way the
    Windows installer names it, so one restore tool finds both. A run that changes
    nothing calls none of this, so it leaves no backup either.
    """

    def __init__(self, now):
        self._now = now
        self._backed_up: set[Path] = set()
        self.backups: list[Path] = []
        self.written: list[Path] = []

    def backup(self, path: Path):
        path = Path(path)
        if path in self._backed_up or not path.exists():
            self._backed_up.add(path)
            return None
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.sidecrab-bak-{stamp}")
        target.write_bytes(path.read_bytes())
        self._backed_up.add(path)
        self.backups.append(target)
        return target

    def write_text(self, path: Path, text: str):
        path = Path(path)
        backup = self.backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The temp sits BESIDE the target so the replace is a same-volume rename and
        # cannot degrade into copy+delete; the '-tmp-' infix is not '-bak-', so a
        # leftover could never be mistaken for a restorable backup.
        tmp = path.with_name(f"{path.name}.sidecrab-tmp-{uuid.uuid4().hex}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        self.written.append(path)
        return backup

    def write_json(self, path: Path, document):
        return self.write_text(path, json_text(document))


def json_text(document) -> str:
    """The one JSON spelling this tool writes: 2-space, unicode kept, one trailing LF."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def read_json_object(path: Path, what: str) -> dict:
    """A JSON object from disk, or {} when the file is absent/empty.

    Malformed is NOT a state: it aborts, because the alternative is overwriting a file
    this tool could not read - which is exactly the operator's own configuration.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupError(f"{path} could not be read ({exc}). {what} was not changed.") from exc
    if not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"{path} is not valid JSON ({exc}). Nothing was written: fix or move the file "
            f"and re-run. {what} was not changed."
        ) from exc
    if not isinstance(document, dict):
        raise SetupError(
            f"{path} is not a JSON object (it holds {type(document).__name__}). Nothing was "
            f"written. {what} was not changed."
        )
    return document


def read_hook_fragment(repo_root: Path) -> dict:
    path = Path(repo_root) / "hooks" / "settings-hooks-fragment-macos.json"
    if not path.exists():
        raise SetupError(f"the hook fragment is missing at {path} - is this a full checkout?")
    fragment = read_json_object(path, "settings.json")
    hooks = fragment.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        raise SetupError(f"{path} carries no hooks object")
    return hooks


# ------------------------------------------------------------------ install: settings.json


@dataclass
class SettingsOutcome:
    """What the settings.json leg decided, before anything is written."""

    settings: dict
    events: int
    allowlist: SettingsPlan
    statusline: str
    save_chain: bool
    prior_statusline: dict | None


def plan_settings(settings: dict, fragment_hooks: dict, statusline: str) -> SettingsOutcome:
    """The whole settings.json change, decided and not yet written. Pure."""
    merged, events = merge_hook_fragment(settings, fragment_hooks)
    allowlist = allowed_hook_urls_plan(merged)
    out = allowlist.settings

    prior = out.get("statusLine")
    prior = prior if isinstance(prior, dict) else None
    prior_command = str(prior.get("command", "")) if prior else ""
    # Saved only when the slot is NOT already ours: re-saving would capture our own
    # command as its own prior and build a chain that calls itself forever.
    save_chain = not statusline_is_ours(prior_command)

    ours = {"type": "command", "command": statusline}
    if prior and "padding" in prior:
        ours["padding"] = prior["padding"]
    out["statusLine"] = ours

    return SettingsOutcome(
        settings=out,
        events=events,
        allowlist=allowlist,
        statusline=statusline,
        save_chain=save_chain,
        prior_statusline=prior if save_chain else None,
    )


def install_settings(env: Environment, writer: Writer, python: str) -> SettingsOutcome:
    """Carry out the settings.json leg. Reads first, so a malformed file aborts early."""
    settings = read_json_object(env.settings_path, "settings.json")
    fragment = read_hook_fragment(env.repo_root)
    script = str(Path(env.repo_root) / "hooks" / "sidecrab_statusline.py")
    outcome = plan_settings(settings, fragment, statusline_command(python, script))

    if outcome.save_chain:
        # Written before settings.json: the chain file is what makes taking the slot
        # reversible, and a crash between the two must not leave the slot taken with
        # no record of what was there.
        writer.write_json(env.chain_path, {"statusLine": outcome.prior_statusline})
        had = outcome.prior_statusline is not None
        env.emit(
            f"  chain:      prior status line saved to {env.chain_path}"
            + ("" if had else " (none existed)")
        )

    if outcome.settings == settings:
        env.emit(f"  settings:   {env.settings_path} already current - not rewritten")
        return outcome

    backup = writer.write_json(env.settings_path, outcome.settings)
    env.emit(f"  hooks:      {outcome.events} event(s) merged into {env.settings_path}")
    env.emit(f"  statusline: {outcome.statusline}")
    env.emit(f"  allowlist:  {outcome.allowlist.reason}")
    if backup:
        env.emit(f"  backup:     {backup}")
    return outcome


# ------------------------------------------------------------------ commands


def command_install(env: Environment, args) -> int:
    python = env.resolve_python()
    env.emit("SideCrab install (macOS)")
    env.emit(f"  repo:       {env.repo_root}")
    env.emit(f"  python:     {python}")

    writer = Writer(env.now)
    install_settings(env, writer, python)
    return 0


COMMANDS = {
    "install": command_install,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sidecrab_setup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="merge hooks, write config, load the LaunchAgents")
    install.add_argument("--with-toast", action="store_true", help="also load the notifier agent")
    approvals = install.add_mutually_exclusive_group()
    approvals.add_argument("--with-approvals", action="store_true")
    approvals.add_argument("--no-approvals", action="store_true")
    install.add_argument(
        "--force-enable",
        action="store_true",
        help="re-enable an agent the operator disabled - the only override",
    )
    install.add_argument("--yes", action="store_true", help="never prompt; take every default")
    return parser


def main(argv=None, env: Environment | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    env = env or Environment.default()
    try:
        return COMMANDS[args.command](env, args)
    except SetupError as exc:
        env.emit(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
