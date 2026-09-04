#!/bin/sh
# Rewrites the LaunchAgent plists from the current checkout, restarts crabd and waits
# for the new version to answer. See sidecrab_setup.py.
set -eu

here=$(CDPATH= cd -- "${0%/*}" && pwd)
. "$here/sidecrab_python.sh"
sidecrab_find_python

exec "$SIDECRAB_PY" "$here/sidecrab_setup.py" update "$@"
