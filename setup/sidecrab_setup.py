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
import plistlib
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

#: Where the operator opens the panel. localhost rather than the dotted form because it
#: is what a browser bar wants and what the README prints.
PANEL_URL = f"http://localhost:{PORT}"

#: crabd requires this header on every POST, so the hook entries carry it and the doctor
#: proves the gate is live by sending one request without it.
PANEL_HEADER = {"X-SideCrab-Panel": "1"}

#: The substring that identifies OUR statusLine, the way HOOK_MARKER identifies our hooks.
STATUSLINE_MARKER = "sidecrab_statusline"

#: The alphabet crabd mints pairing codes from: no I, L, O or U, so a code read off a
#: screen and typed back cannot turn into a different code.
PAIRING_CODE_RE = re.compile(r"^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{10}$")

#: A long-lived Claude token (`claude setup-token`). Shape only - it is never decoded.
LIMITS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,512}$")
LIMITS_TOKEN_PREFIX = "sk-ant-"

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


def _default_read_secret(prompt: str) -> str:
    """A secret, off a pipe when there is one and off the terminal otherwise.

    Never an argument: a token on the command line is in every `ps` and in the shell
    history of whoever ran it.
    """
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass(prompt)
    return sys.stdin.readline()


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
    read_secret: callable = staticmethod(_default_read_secret)
    #: None means "use the lazy crabd import"; the suite injects a recorder instead so
    #: no test ever writes to the developer's Keychain.
    store_token: callable | None = None

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


# ------------------------------------------------------------------ install: config.json


#: What an operator is told before panel approvals are switched on. Three promises, each
#: one a property of crabd's permission broker rather than a hope about this installer.
APPROVAL_GUARANTEES = (
    "crabd holds each permission prompt for at most 55 s and NEVER auto-allows",
    "no tap, a timeout, or approvals off all fall back to the terminal dialog",
    "a tap is only honoured with the pairing code - print it with install.sh --pairing-code",
)


@dataclass(frozen=True)
class ApprovalsDecision:
    """Whether panelApprovals.enabled is written, to what, and why. Pure."""

    enabled: bool
    write: bool
    reason: str


def approvals_decision(requested, current) -> ApprovalsDecision:
    """``requested`` is True/False when the operator said so, None for the default.

    The default writes ``false`` ONLY when the key is absent. A plain re-run must never
    revert an operator who chose --with-approvals earlier: the switch is theirs, and a
    silent revert would take away a control surface they believe is armed.
    """
    if requested is None:
        if current is None:
            return ApprovalsDecision(False, True, "default OFF (key absent; --with-approvals to enable)")
        return ApprovalsDecision(bool(current), False, f"left as {bool(current)} - your choice, not a default")
    if current is not None and bool(current) == requested:
        return ApprovalsDecision(requested, False, f"already {requested}")
    return ApprovalsDecision(requested, True, f"set to {requested}")


def ask_approvals(env: Environment, args) -> bool | None:
    """The operator's answer, or None for "take the default". Impure only in the prompt."""
    if args.with_approvals:
        return True
    if args.no_approvals:
        return False
    if args.yes or env.ask is None:
        return None
    env.emit("")
    env.emit("  Panel approvals let a tap in the browser panel allow or deny a tool call.")
    for line in APPROVAL_GUARANTEES:
        env.emit(f"    - {line}")
    answer = (env.ask("  Enable panel approvals? [y/N] ") or "").strip().lower()
    return True if answer in ("y", "yes") else None


def install_config(env: Environment, writer: Writer, args) -> ApprovalsDecision:
    config = read_json_object(env.config_path, "config.json")
    current = None
    block = config.get("panelApprovals")
    if isinstance(block, dict) and "enabled" in block:
        current = bool(block["enabled"])

    decision = approvals_decision(ask_approvals(env, args), current)
    if decision.write:
        out = copy.deepcopy(config)
        out["panelApprovals"] = {"enabled": decision.enabled}
        backup = writer.write_json(env.config_path, out)
        if backup:
            env.emit(f"  backup:     {backup}")
    env.emit(f"  approvals:  {decision.reason}")
    if decision.enabled:
        env.emit("  SECURITY: panel approvals are ON.")
        for line in APPROVAL_GUARANTEES:
            env.emit(f"    - {line}")
    return decision


# ------------------------------------------------------------------ LaunchAgents


@dataclass(frozen=True)
class AgentSpec:
    """One LaunchAgent, described once. Adding a component is one row in AGENTS."""

    key: str
    label: str
    script: str
    #: 0 = this component owns no port. Which one does is a fact of the catalogue,
    #: never a guess made at the call site.
    port: int


AGENTS = (
    AgentSpec("crabd", "com.sidecrab.crabd", "companion/crabd.py", PORT),
    AgentSpec("toast", "com.sidecrab.toast", "notifier/sidecrab_toast.py", 0),
)

#: A LaunchAgent inherits launchd's PATH, not the operator's login PATH. Pinned to the
#: system directories so the agents never pick up a shim from a shell profile.
AGENT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: Polls, not wall clock: the sleep is injectable, and a clock-based deadline under a
#: neutered sleep either spins forever or gives up after one attempt.
HEALTH_WAIT_POLLS = 20
HEALTH_POLL_SEC = 0.5


def agent_spec(key: str) -> AgentSpec:
    for spec in AGENTS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def plist_document(spec: AgentSpec, python: str, repo_root, logs_dir) -> dict:
    """The LaunchAgent property list, as a dict. Pure.

    No ProcessType key on purpose: whether App Nap or timer coalescing touches a
    KeepAlive agent on this hardware is UNMEASURED (docs/GETTING-STARTED-MACOS-NOTES.md
    carries the command to measure it), and a guessed value would read as a finding.
    """
    log = str(Path(logs_dir) / f"{spec.label}.log")
    return {
        "Label": spec.label,
        "ProgramArguments": [python, str(Path(repo_root) / spec.script)],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": AGENT_PATH},
    }


def parse_disabled(text: str) -> set[str]:
    """The labels `launchctl print-disabled` reports as disabled. Pure.

    Only ``=> true`` counts: the answer lists every label launchd knows a state for,
    and reading a ``false`` row as disabled would park a healthy agent.
    """
    return set(re.findall(r'"([^"]+)"\s*=>\s*true', text or ""))


@dataclass(frozen=True)
class AgentState:
    label: str
    loaded: bool
    pid: int | None
    state: str


def parse_agent_state(label: str, code: int, out: str, err: str) -> AgentState:
    """`launchctl print gui/<uid>/<label>` as a state. Pure, and never an error.

    A label that is not loaded answers non-zero with the measured
    ``Could not find service "..." in domain for user gui: 501`` - a state to report,
    not a failure to raise.
    """
    if code != 0:
        return AgentState(label, loaded=False, pid=None, state="absent")
    state = "unknown"
    match = re.search(r"^\s*state\s*=\s*(.+?)\s*$", out or "", re.MULTILINE)
    if match:
        state = match.group(1)
    pid = None
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", out or "", re.MULTILINE)
    if match:
        pid = int(match.group(1))
    return AgentState(label, loaded=True, pid=pid, state=state)


@dataclass(frozen=True)
class PortHolder:
    pid: int
    command: str


def parse_port_holders(text: str) -> list[PortHolder]:
    """`lsof -nP -iTCP:<port> -sTCP:LISTEN` rows. Pure.

    Health-by-HTTP cannot tell WHO answered: after a failed restart a stray process can
    hold the port and answer convincingly while the agent itself is dead. The PID is the
    only thing that separates our daemon from something wearing its clothes.
    """
    holders = []
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[1].isdigit():
            continue
        holders.append(PortHolder(pid=int(fields[1]), command=fields[0]))
    return holders


def format_port_holders(holders, port: int) -> str:
    if not holders:
        return f"no listener found on port {port}"
    return ", ".join(f"PID {h.pid} ({h.command})" for h in holders)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    ok: bool
    reason: str


def service_verdict(health_ok: bool, agent_state: str, holders, port: int) -> Verdict:
    """Is crabd actually up? Answered by BOTH readings, never by health alone. Pure.

    An answer with no running agent is not "fine": it is the loudest row on the table,
    because that process is also what stops the real instance from ever binding.
    """
    running = agent_state == "running"
    where = f"the agent is {agent_state}"

    if health_ok and running:
        return Verdict("ok", True, f"crabd answered and the agent is running - it owns port {port}")
    if health_ok:
        return Verdict(
            "foreign-answerer",
            False,
            f"something answered on port {port} but {where} - a health answer is NOT proof the "
            f"agent is up. Port {port} is held by {format_port_holders(holders, port)}: a foreign "
            "process, or an orphan from a failed restart, which is also what stops the real "
            "instance binding.",
        )
    if running:
        return Verdict(
            "not-answering",
            False,
            f"the agent is running but nothing answered on port {port} - still starting, or up "
            "and unbound",
        )
    return Verdict("down", False, f"nothing answered on port {port} and {where} - crabd is not running")


def task_enable_decision(registered: bool, prior_disabled: bool, force_enable: bool) -> Decision:
    """Should a re-registration leave the agent DISABLED, and should it be started? Pure.

    Re-registering a disabled agent is fine and keeps its interpreter and paths current.
    STARTING it, or enabling it, overturns a decision the operator made deliberately -
    and --force-enable is the only way to overturn it.

    Keyed on the DISABLED LIST alone, not on whether our plist happens to be there:
    `launchctl disable` is a per-domain override that outlives the plist, so a label the
    operator disabled and then lost the plist for is still a label they said no to.
    """
    was_disabled = prior_disabled
    if was_disabled and not force_enable:
        return Decision(
            "leave-disabled",
            False,
            "was DISABLED - the plist was refreshed and it was left disabled "
            "(--force-enable to override)",
        )
    if was_disabled:
        return Decision("force-enabled", True, "was DISABLED - re-enabled by --force-enable")
    return Decision("load", True, "re-registered" if registered else "newly registered")


# -- the impure launchctl wrappers


def launchctl(env: Environment, *argv, timeout=30):
    return env.run(["launchctl", *argv], timeout=timeout)


def disabled_labels(env: Environment) -> set[str]:
    code, out, _err = launchctl(env, "print-disabled", f"gui/{env.uid}")
    return parse_disabled(out) if code == 0 else set()


def agent_state(env: Environment, label: str) -> AgentState:
    code, out, err = launchctl(env, "print", f"gui/{env.uid}/{label}")
    return parse_agent_state(label, code, out, err)


def port_holders(env: Environment, port: int) -> list[PortHolder]:
    # lsof exits 1 with no output when nothing matches; an absent listener is a state.
    _code, out, _err = env.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=10
    )
    return parse_port_holders(out)


def ensure_logs_dir(env: Environment) -> Path:
    # 0700: the agents' stdout carries session titles and repo paths.
    env.logs_dir.mkdir(parents=True, exist_ok=True)
    env.logs_dir.chmod(0o700)
    return env.logs_dir


def load_agent(env: Environment, spec: AgentSpec, python: str, disabled: set[str], force: bool) -> Decision:
    """Write the plist, then load it - unless the operator disabled this label."""
    plist_path = env.agents_dir / f"{spec.label}.plist"
    decision = task_enable_decision(plist_path.exists(), spec.label in disabled, force)

    document = plistlib.dumps(plist_document(spec, python, env.repo_root, env.logs_dir))
    env.agents_dir.mkdir(parents=True, exist_ok=True)
    if not plist_path.exists() or plist_path.read_bytes() != document:
        plist_path.write_bytes(document)

    if decision.action == "leave-disabled":
        env.emit(f"  agent:      {spec.label} {decision.reason}")
        return decision
    if decision.action == "force-enabled":
        launchctl(env, "enable", f"gui/{env.uid}/{spec.label}")
    # bootout first: bootstrapping a label that is already loaded fails, and its
    # "not found" exit for a label that is not is exactly what we want to ignore.
    launchctl(env, "bootout", f"gui/{env.uid}/{spec.label}")
    launchctl(env, "bootstrap", f"gui/{env.uid}", str(plist_path))
    env.emit(f"  agent:      {spec.label} {decision.reason}")
    return decision


def selected_agents(args) -> list[AgentSpec]:
    return [spec for spec in AGENTS if spec.key == "crabd" or getattr(args, "with_toast", False)]


def install_agents(env: Environment, python: str, args) -> None:
    ensure_logs_dir(env)
    disabled = disabled_labels(env)
    for spec in selected_agents(args):
        load_agent(env, spec, python, disabled, getattr(args, "force_enable", False))


# ------------------------------------------------------------------ update / restart


def read_crabd_version(repo_root) -> str | None:
    """The VERSION the checkout would serve, so the wait knows what it is waiting for."""
    path = Path(repo_root) / "companion" / "crabd.py"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def health_version(env: Environment) -> str | None:
    status, body = env.http_get(f"{BASE_URL}/v1/health", timeout=3)
    if status != 200:
        return None
    try:
        return str(json.loads(body).get("version") or "") or None
    except (json.JSONDecodeError, AttributeError):
        return None


def wait_for_version(env: Environment, expected: str | None, polls: int = HEALTH_WAIT_POLLS):
    """Poll /v1/health until the expected version answers. Returns (ok, last_seen)."""
    seen = None
    for attempt in range(polls):
        seen = health_version(env)
        if seen and (expected is None or seen == expected):
            return True, seen
        if attempt + 1 < polls:
            env.sleep(HEALTH_POLL_SEC)
    return False, seen


def refuse_if_foreign_holder(env: Environment, spec: AgentSpec) -> None:
    """Never start over a process we do not own. Raises rather than starting blind.

    Starting blind is what produced a dark panel on Windows: the restart "succeeded",
    the new process lost the bind race and exited 1, and the only trace was an exit
    code on an agent that read as fine.
    """
    if spec.port <= 0:
        return
    holders = port_holders(env, spec.port)
    if not holders:
        return
    ours = agent_state(env, spec.label)
    if ours.pid is not None and all(h.pid == ours.pid for h in holders):
        return
    raise SetupError(
        f"{spec.label} was NOT restarted: port {spec.port} is held by "
        f"{format_port_holders(holders, spec.port)}, which is not this agent. Starting now "
        "would lose the bind race - crabd refuses to share the port, so the new process "
        "would exit and serve nothing. Stop that process (kill <pid>) and re-run."
    )


# ------------------------------------------------------------------ pairing code, limits token


def format_pairing_code(raw) -> str | None:
    """The minted code as ``XXXXX-XXXXX``, or None when the file holds no usable code. Pure."""
    code = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    if not PAIRING_CODE_RE.match(code):
        return None
    return f"{code[:5]}-{code[5:]}"


def validate_limits_token(token) -> str:
    """The token, or a refusal naming what is wrong with it. Pure - never logs the value."""
    value = str(token or "").strip()
    if not value:
        raise SetupError("The token is empty - nothing was stored.")
    if not value.startswith(LIMITS_TOKEN_PREFIX):
        raise SetupError(
            f"That does not look like a Claude token: it does not start with "
            f"{LIMITS_TOKEN_PREFIX}. Run `claude setup-token` and paste what it prints."
        )
    if len(value) < 20:
        raise SetupError("That token is shorter than 20 characters - nothing was stored.")
    if not LIMITS_TOKEN_RE.match(value):
        raise SetupError(
            "That token holds characters a Claude token does not (expected A-Z a-z 0-9 _ -)."
        )
    return value


def default_store_token(env: Environment, token: str) -> bool:
    """Hand the token to crabd's own store. Imported lazily: this is the only path that
    needs companion/, and a setup run that never stores a token must not pay for it."""
    companion = str(Path(env.repo_root) / "companion")
    if companion not in sys.path:
        sys.path.insert(0, companion)
    import crabd  # noqa: PLC0415 - lazy on purpose

    platform = getattr(crabd, "PLATFORM")
    return bool(platform.store_limits_token(token))


# ------------------------------------------------------------------ commands


def command_pairing_code(env: Environment, args) -> int:
    if not env.token_path.exists():
        env.emit(
            f"No pairing code at {env.token_path}. Start crabd first - it mints the code "
            "on its first start."
        )
        return 1
    code = format_pairing_code(env.token_path.read_text(encoding="utf-8", errors="replace"))
    if code is None:
        env.emit(
            f"{env.token_path} does not hold a usable pairing code. Stop crabd, delete the "
            "file and start crabd again to mint a new one."
        )
        return 1
    env.emit(f"  pairing code: {code}")
    env.emit(f"  Enter it in the panel's settings sheet at {PANEL_URL}.")
    return 0


def command_limits_token(env: Environment, args) -> int:
    """Store a long-lived Claude token for crabd's limit gauges.

    The value is read from stdin (or the terminal), never from argv, and never printed
    back - not on success, not in an error, not in `status`.
    """
    token = validate_limits_token(env.read_secret("Paste the token (claude setup-token): "))
    store = env.store_token or (lambda value: default_store_token(env, value))
    try:
        stored = store(token)
    except (AttributeError, ImportError) as exc:
        raise SetupError(
            f"this crabd cannot store a long-lived token yet ({exc}). "
            "Update crabd to a build that carries PLATFORM.store_limits_token."
        ) from exc
    if not stored:
        raise SetupError("crabd refused to store the token - see its log.")
    env.emit("  limits token: stored. Restart crabd to pick it up.")
    return 0


def command_update(env: Environment, args) -> int:
    python = env.resolve_python()
    spec = agent_spec("crabd")
    env.emit("SideCrab update (macOS)")
    env.emit(f"  repo:       {env.repo_root}")
    env.emit(f"  python:     {python}")

    # Before anything is started: a foreign holder is a different problem from a slow
    # shutdown, and the PID is the only thing that tells them apart.
    refuse_if_foreign_holder(env, spec)

    ensure_logs_dir(env)
    disabled = disabled_labels(env)
    for candidate in AGENTS:
        if candidate.key == "crabd" or (env.agents_dir / f"{candidate.label}.plist").exists():
            load_agent(env, candidate, python, disabled, force=False)

    expected = read_crabd_version(env.repo_root)
    launchctl(env, "kickstart", "-k", f"gui/{env.uid}/{spec.label}")
    ok, seen = wait_for_version(env, expected)
    if not ok:
        env.emit(
            f"  health:     crabd is serving {seen or 'nothing'} after "
            f"{HEALTH_WAIT_POLLS} polls; this checkout is {expected}. "
            f"Check {env.logs_dir / (spec.label + '.log')}."
        )
        return 1
    env.emit(f"  health:     crabd {seen} answering on {BASE_URL}")
    return 0


def command_install(env: Environment, args) -> int:
    python = env.resolve_python()
    env.emit("SideCrab install (macOS)")
    env.emit(f"  repo:       {env.repo_root}")
    env.emit(f"  python:     {python}")

    writer = Writer(env.now)
    # Order is the safety argument: settings.json is read first, so a file this tool
    # cannot parse aborts before anything at all has been written or loaded.
    install_settings(env, writer, python)
    install_config(env, writer, args)
    install_agents(env, python, args)
    env.emit(f"  panel:      {BASE_URL}")
    return 0


COMMANDS = {
    "install": command_install,
    "update": command_update,
    "pairing-code": command_pairing_code,
    "limits-token": command_limits_token,
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

    sub.add_parser("update", help="refresh the plists from this checkout and restart crabd")
    sub.add_parser("pairing-code", help="print the code crabd minted, and where to enter it")
    # No token argument: the value is read from stdin so it never lands in argv.
    sub.add_parser("limits-token", help="store a long-lived Claude token, read from stdin")
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
