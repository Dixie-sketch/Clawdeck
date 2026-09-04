#!/bin/sh
# Removes SideCrab's LaunchAgents, its settings.json entries and its status line,
# restoring whatever was there before. See sidecrab_setup.py.
#
#   ./uninstall.sh [--purge] [--yes]
set -eu

here=$(CDPATH= cd -- "${0%/*}" && pwd)
. "$here/sidecrab_python.sh"
sidecrab_find_python

exec "$SIDECRAB_PY" "$here/sidecrab_setup.py" uninstall "$@"
