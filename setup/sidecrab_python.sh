# Interpreter search for the SideCrab sh entry points. Sourced, never executed.
#
# This has to run before any SideCrab Python does, so it is a second copy of the rule in
# sidecrab_setup.py's python_candidates/choose_python. Keep the two in step: the order,
# the floor and the message are asserted from both sides.
#
# Sets SIDECRAB_PY to an absolute interpreter path, or prints why not and returns 1.

# The floor. Anything older has never run this code.
SIDECRAB_PY_MIN_MAJOR=3
SIDECRAB_PY_MIN_MINOR=13

# Newest name first, so a deliberately installed 3.14 beats whatever python3 points at.
SIDECRAB_PY_NAMES='python3.14 python3.13 python3'

# Searched after PATH: a LaunchAgent's PATH is not the login PATH, and brew installs here.
# Overridable (colon-separated, empty for none) so a test can run with no Homebrew at all.
: "${SIDECRAB_PYTHON_DIRS=/opt/homebrew/bin:/usr/local/bin}"

sidecrab_py_probe() {
    # Prints "<major>.<minor>" for a usable interpreter. Apple's /usr/bin/python3 stub
    # exits non-zero here with a "No developer tools were found" note; that is the whole
    # reason this probes rather than trusting the file's presence.
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

sidecrab_py_ok() {
    _v=$(sidecrab_py_probe "$1") || return 1
    [ -n "$_v" ] || return 1
    _maj=${_v%%.*}
    _min=${_v#*.}
    _min=${_min%%.*}
    case "$_maj$_min" in *[!0-9]*) return 1 ;; esac
    [ "$_maj" -gt "$SIDECRAB_PY_MIN_MAJOR" ] && return 0
    [ "$_maj" -eq "$SIDECRAB_PY_MIN_MAJOR" ] && [ "$_min" -ge "$SIDECRAB_PY_MIN_MINOR" ]
}

sidecrab_py_absolute() {
    # $SIDECRAB_PYTHON as an absolute path, or empty. The plist stores whatever this
    # resolves to and a LaunchAgent has no working directory, so `python3.13` and
    # `./python3` must both end up absolute. Same rule as absolute_override().
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        */*) printf '%s\n' "$(pwd)/${1#./}" ;;
        *) command -v -- "$1" 2>/dev/null | while IFS= read -r _p; do
               case "$_p" in /*) printf '%s\n' "$_p" ;; esac
           done ;;
    esac
}

sidecrab_find_python() {
    if [ -n "${SIDECRAB_PYTHON:-}" ]; then
        _abs=$(sidecrab_py_absolute "$SIDECRAB_PYTHON")
        if [ -n "$_abs" ] && [ -x "$_abs" ] && sidecrab_py_ok "$_abs"; then
            SIDECRAB_PY=$_abs
            return 0
        fi
        if [ -z "$_abs" ] || [ ! -x "$_abs" ]; then
            echo "SIDECRAB_PYTHON=$SIDECRAB_PYTHON did not resolve to an executable file." >&2
            echo "  Give an absolute path, or a name that is on PATH - the LaunchAgent stores" >&2
            echo "  the path it resolves to and has no working directory to resolve against." >&2
            return 1
        fi
        echo "SIDECRAB_PYTHON=$SIDECRAB_PYTHON ($_abs) is not a usable Python" >&2
    fi
    _dirs="${PATH}:${SIDECRAB_PYTHON_DIRS}"
    for _name in $SIDECRAB_PY_NAMES; do
        _rest=$_dirs
        while [ -n "$_rest" ]; do
            _dir=${_rest%%:*}
            case "$_rest" in *:*) _rest=${_rest#*:} ;; *) _rest= ;; esac
            [ -n "$_dir" ] || continue
            _cand="${_dir%/}/$_name"
            [ -x "$_cand" ] || continue
            if sidecrab_py_ok "$_cand"; then
                SIDECRAB_PY=$_cand
                return 0
            fi
        done
    done
    # echo, not cat: this is the one path that runs with whatever PATH the caller had,
    # and a failed search must not itself fail for want of an external command.
    echo "No usable Python found. SideCrab needs ${SIDECRAB_PY_MIN_MAJOR}.${SIDECRAB_PY_MIN_MINOR} or newer." >&2
    echo "  Fix: install Python 3.13 or newer (brew install python@3.13, or python.org) - the" >&2
    echo "  LaunchAgent stores an absolute interpreter path, so it must be a real install." >&2
    echo "  Apple's /usr/bin/python3 is a command-line-tools stub and does not count." >&2
    echo "  Or point SIDECRAB_PYTHON at the interpreter you want." >&2
    return 1
}
