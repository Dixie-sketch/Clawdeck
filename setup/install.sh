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
# script; each maps onto the sidecrab_setup.py command of the same name. They are
# handled HERE, so argparse's help below cannot mention them - hence the four lines.
cmd=install
case "${1:-}" in
    --status)       cmd=status;       shift ;;
    --doctor)       cmd=doctor;       shift ;;
    --pairing-code) cmd=pairing-code; shift ;;
    --limits-token) cmd=limits-token; shift ;;
    --help|-h)
        echo "install.sh [install options]   merge hooks, write config, load the agents"
        echo "install.sh --status            read-only: what is installed and answering"
        echo "install.sh --doctor            a PASS/FAIL table over the whole chain"
        echo "install.sh --pairing-code      print the code crabd minted"
        echo "install.sh --limits-token      store a long-lived token, read from stdin"
        echo ""
        ;;
esac

exec "$SIDECRAB_PY" "$here/sidecrab_setup.py" "$cmd" "$@"
