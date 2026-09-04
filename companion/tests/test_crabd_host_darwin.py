"""`host` on macOS: the mach counters, and the arithmetic that hands them to HostSampler.

HostSampler is not changed by this file and must not be: its rules (kernel INCLUDES
idle, the first sample has no delta, CPU_MIN_TOTAL_TICKS, the A-07/A-08/A-09 branches,
no last-good cache) were bought by measurement on Windows and are the contract's failure
table. What is new is a reader that speaks mach and answers in the sampler's units, so
everything below is about the ADAPTER: the tuple convention, the 32-bit unwrap, and the
Activity Monitor memory formula.

Every test here is pure and runs on any OS - the kernel is behind injected seams - except
the two at the bottom that are the healthy-night check against a real Mac.
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation every other companion test module takes: these globals
    name REAL files under ~, and a reader built without an explicit path would otherwise
    reach the operator's live store (the limits cache was poisoned exactly this way on
    2026-08-26)."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE,
                             crabd.LIMITS_TOKEN_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"
    crabd.LIMITS_TOKEN_FILE = root / "no-such-limits-token.dpapi"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE) = setUpModule.originals
    _MODULE_TMP.cleanup()


class ModuleIsolationTests(unittest.TestCase):
    def test_every_global_naming_a_real_file_points_into_the_sandbox(self):
        sandbox = Path(_MODULE_TMP.name)
        for name in ("LIMITS_CACHE_FILE", "USER_CONFIG_FILE", "HISTORY_FILE",
                     "CREDENTIALS_FILE", "LIMITS_TOKEN_FILE"):
            with self.subTest(global_name=name):
                self.assertEqual(getattr(crabd, name).parent, sandbox)


# ------------------------------------------------------------- the tuple convention

class DarwinCpuTupleTests(unittest.TestCase):
    """`host_statistics(HOST_CPU_LOAD_INFO)` counts (user, system, idle, nice) in
    CLK_TCK ticks; HostSampler wants (idle, kernel, user) in 100 ns units with KERNEL
    INCLUDING IDLE. This is the whole of the translation, and it is where a mistake
    would be silent rather than loud."""

    def test_the_raw_mach_ticks_become_the_samplers_idle_kernel_user(self):
        platform = crabd.DarwinPlatform(load_info=lambda: (100, 100, 300, 0),
                                        clk_tck=100)
        # 100 ns units per tick at 100 Hz = 100_000.
        self.assertEqual(platform.cpu_times(),
                         (30_000_000, 40_000_000, 10_000_000))


_MEM = (32 * 1024 ** 3, 12 * 1024 ** 3)      # a memory reading that always succeeds


def scripted(readings):
    """A load_info seam that walks a list of raw (user, system, idle, nice) tuples."""
    queue = list(readings)
    return lambda: queue.pop(0)


def sampler_over(reader):
    """A HostSampler fed by `reader`, with memory that always answers. NullPlatform
    underneath so nothing here can reach a real kernel on the host running the suite."""
    return crabd.HostSampler(times=reader, memory=lambda: _MEM,
                             platform=crabd.NullPlatform())


class DarwinCpuThroughTheSamplerTests(unittest.TestCase):
    """The translation meeting the sampler it was written for. The mutants are the
    point: both are one word from the shipping code, both look right, and both produce
    a gauge that is DEAD rather than wrong - which is the failure nobody notices."""

    RAW = [(1_000, 1_000, 5_000, 0), (1_100, 1_100, 5_300, 0)]   # delta 100/100/300/0

    def test_a_baseline_and_a_delta_give_the_busy_fraction(self):
        # kernel 400, user 100 ticks -> total 500, idle 300 -> busy 200/500.
        sampler = sampler_over(crabd.DarwinPlatform(load_info=scripted(self.RAW),
                                                    clk_tck=100).cpu_times)
        self.assertIsNone(sampler.sample()["cpuPct"])       # first sample, no delta yet
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)

    def test_a_reader_that_does_not_fold_idle_into_kernel_is_a_dead_gauge(self):
        """MUTANT: `system` handed on as kernel. HostSampler's busy fraction is
        ((kernel + user) - idle) / (kernel + user) because GetSystemTimes' kernel time
        INCLUDES idle; unfolded, idle (300) exceeds the total (200) on any mostly-idle
        machine, which is A-08, and the sampler serves null. Not once - every pass, on a
        perfectly healthy Mac. That is why the fold is in cpu_times and not left to the
        sampler."""
        raw = scripted(self.RAW)

        def unfolded():
            user, system, idle, nice = raw()
            return (idle * 100_000, system * 100_000, (user + nice) * 100_000)

        sampler = sampler_over(unfolded)
        self.assertIsNone(sampler.sample()["cpuPct"])
        self.assertIsNone(sampler.sample()["cpuPct"])


class DarwinNiceIsBusyTests(unittest.TestCase):
    """`nice` is CPU time spent running low-priority USER processes: busy time, and it
    belongs with `user`. A machine running a nice'd build is not idle."""

    RAW = [(1_000, 1_000, 5_000, 1_000), (1_100, 1_100, 5_300, 1_100)]

    def test_nice_ticks_count_as_busy(self):
        # kernel 400, user 100 + nice 100 = 200 -> total 600, idle 300 -> busy 300/600.
        sampler = sampler_over(crabd.DarwinPlatform(load_info=scripted(self.RAW),
                                                    clk_tck=100).cpu_times)
        self.assertIsNone(sampler.sample()["cpuPct"])
        cpu = sampler.sample()["cpuPct"]
        self.assertEqual(cpu, 50.0)
        # The plausible wrong answer, named: dropping nice gives total 500 and busy
        # 200/500. It is not an error anywhere - just a machine reported four fifths as
        # busy as it is, for as long as anything runs at nice priority.
        self.assertNotEqual(cpu, 40.0)


if __name__ == "__main__":
    unittest.main()
