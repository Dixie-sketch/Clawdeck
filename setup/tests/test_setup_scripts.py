"""The three sh entry points, executed for real against a controlled PATH.

They run BEFORE any Python exists, so their interpreter search cannot be unit-tested
through the module - it has to be the shell that runs. Every case here uses a fake
python3 in a temp directory, a temp HOME, and no Homebrew prefixes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest

from _harness import SETUP_DIR, TempHome


class ShellWrapper(TempHome):
    def _fake_python(self, version: str, argv_log=None) -> str:
        """A stand-in interpreter: answers the version probe, records what it is asked to run."""
        bindir = self.home.parent / "fakebin"
        bindir.mkdir(exist_ok=True)
        path = bindir / "python3"
        log = argv_log or "/dev/null"
        path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then printf \'%s\\n\'; exit 0; fi\n' % version
            + 'printf "%%s\\n" "$@" >> "%s"\n' % log
            + "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(bindir)

    def _run(self, script: str, *args, bindir: str):
        env = {
            "PATH": bindir,
            "HOME": str(self.home),
            # No Homebrew prefixes: this case is "the only python3 is the fake one".
            "SIDECRAB_PYTHON_DIRS": "",
        }
        return subprocess.run(
            ["/bin/sh", str(SETUP_DIR / script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_an_old_python_is_refused_and_the_message_names_the_fix(self):
        bindir = self._fake_python("3.9")
        result = self._run("install.sh", "--status", bindir=bindir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("brew install python@3.13", result.stdout + result.stderr)

    def test_a_supported_python_runs_the_setup_module_with_the_mapped_command(self):
        log = str(self.home.parent / "argv.log")
        bindir = self._fake_python("3.13", argv_log=log)
        result = self._run("install.sh", "--status", bindir=bindir)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = open(log, encoding="utf-8").read().split()
        self.assertEqual(recorded, [str(SETUP_DIR / "sidecrab_setup.py"), "status"])

    def test_every_wrapper_maps_onto_its_own_command(self):
        for script, expected in (
            ("install.sh", "install"),
            ("update.sh", "update"),
            ("uninstall.sh", "uninstall"),
        ):
            with self.subTest(script=script):
                log = str(self.home.parent / f"argv-{script}.log")
                bindir = self._fake_python("3.14", argv_log=log)
                result = self._run(script, bindir=bindir)
                self.assertEqual(result.returncode, 0, result.stderr)
                recorded = open(log, encoding="utf-8").read().split()
                self.assertEqual(recorded[1], expected)

    def test_sidecrab_python_overrides_the_search(self):
        log = str(self.home.parent / "argv-override.log")
        bindir = self._fake_python("3.13", argv_log=log)
        env = {"PATH": "/nonexistent", "HOME": str(self.home), "SIDECRAB_PYTHON_DIRS": ""}
        env["SIDECRAB_PYTHON"] = os.path.join(bindir, "python3")
        result = subprocess.run(
            ["/bin/sh", str(SETUP_DIR / "install.sh"), "--doctor"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(open(log, encoding="utf-8").read().split()[1], "doctor")


if __name__ == "__main__":
    unittest.main()
