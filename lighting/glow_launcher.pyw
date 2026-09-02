r"""Launcher for sidecrab_glow under a Scheduled Task.

This used to spawn a child python.exe in a hidden console, because the cuesdk
handshake looked console-dependent: pythonw died at 0xC000001D where interactive
python.exe degraded cleanly. That reading was wrong, and the workaround never
worked — every console variant crashed too, just with a different NTSTATUS
(0xC000001D / 0xC0000005 / 0xC0000096).

The real bug was a use-after-free in `icue.IcueAdapter.connect()`: the CueSdk
object was a local that only reached `self` on the success path, so a failed
handshake dropped the ctypes thunk the native SDK keeps calling ~2x/second.
Consoles had nothing to do with it (proven 2026-08-26 — see icue.py's docstring).

So this file is now a plain in-process entry point: no child, no console games.
It exists only because setup\SideCrab.Common.ps1 registers this path, and because
.pyw keeps a window from flashing at logon. One process means the task's
restart-on-failure sees glow's own exit code rather than a relay's.
"""

import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))

from sidecrab_glow import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
