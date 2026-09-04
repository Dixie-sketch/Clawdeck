#!/bin/sh
# SideCrab installer for macOS. Thin on purpose: find a Python, hand over to
# sidecrab_setup.py, which holds every decision this makes.
#
#   ./install.sh [--with-toast] [--with-approvals|--no-approvals] [--force-enable] [--yes]
#   ./install.sh --status | --doctor | --pairing-code | --limits-token
set -eu

here=$(CDPATH= cd -- "${0%/*}" && pwd)
. "$here/sidecrab_python.sh"
sidecrab_find_python

# The read-only side commands are spelled as flags here so the README documents one
# script; each maps onto the sidecrab_setup.py command of the same name.
cmd=install
case "${1:-}" in
    --status)       cmd=status;       shift ;;
    --doctor)       cmd=doctor;       shift ;;
    --pairing-code) cmd=pairing-code; shift ;;
    --limits-token) cmd=limits-token; shift ;;
esac

exec "$SIDECRAB_PY" "$here/sidecrab_setup.py" "$cmd" "$@"
