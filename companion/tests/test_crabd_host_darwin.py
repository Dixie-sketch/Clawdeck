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
import json
import subprocess
import sys
import tempfile
import threading
import time
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


class DarwinCounterWrapTests(unittest.TestCase):
    """The mach tick counters are natural_t - 32 bits - and cumulative since boot.

    MEASURED: about 1600 ticks a second summed across 16 cores at CLK_TCK 100, so a
    bucket crosses 2^32 after roughly 31 days of uptime. Handed on raw, that is one
    backwards jump per bucket per month, and HostSampler answers a backwards counter by
    re-baselining and serving null - a gauge that blanks for a pass on a schedule nobody
    would ever connect to uptime. Unwrapping here makes the wrap invisible upstream.
    """

    #: idle 4_294_967_000 -> 200 is a wrap: 296 ticks to the modulus plus 200 past it.
    WRAPPED = [(1_000, 1_000, 4_294_967_000, 0), (1_100, 1_100, 200, 0)]

    def test_a_wrapped_counter_reads_as_a_forward_delta(self):
        platform = crabd.DarwinPlatform(load_info=scripted(self.WRAPPED), clk_tck=100)
        first, second = platform.cpu_times(), platform.cpu_times()
        self.assertEqual(second[0] - first[0], 496 * 100_000)     # idle, in 100 ns

    def test_the_sampler_serves_a_number_across_a_wrap_instead_of_blanking(self):
        # idle 496, kernel 100 + 496 = 596, user 100 -> total 696, busy 200.
        sampler = sampler_over(
            crabd.DarwinPlatform(load_info=scripted(self.WRAPPED),
                                 clk_tck=100).cpu_times)
        self.assertIsNone(sampler.sample()["cpuPct"])           # first sample
        self.assertEqual(sampler.sample()["cpuPct"], 28.7)

    def test_two_buckets_wrapping_in_the_same_reading_are_independent(self):
        """One lap counter per bucket. A single shared one would credit the lap to
        whichever bucket was checked first and leave the other still going backwards."""
        raw = [(4_294_967_200, 1_000, 4_294_967_000, 0), (100, 1_100, 200, 0)]
        platform = crabd.DarwinPlatform(load_info=scripted(raw), clk_tck=100)
        first, second = platform.cpu_times(), platform.cpu_times()
        self.assertEqual(second[0] - first[0], 496 * 100_000)   # idle wrapped
        # user 4_294_967_200 -> 100 is 96 ticks to the modulus plus 100 past it; the
        # kernel column carries the idle wrap and the system ticks together.
        self.assertEqual(second[2] - first[2], 196 * 100_000)   # user wrapped too
        self.assertEqual(second[1] - first[1], (100 + 496) * 100_000)

    def test_an_unwrapped_bucket_is_untouched(self):
        """The unwrap must be invisible when nothing wraps: a lap added to a counter
        that only went forwards would be a 2^32-tick delta out of nowhere."""
        raw = [(1_000, 1_000, 5_000, 0), (1_100, 1_100, 5_300, 0),
               (1_200, 1_200, 5_600, 0)]
        platform = crabd.DarwinPlatform(load_info=scripted(raw), clk_tck=100)
        readings = [platform.cpu_times() for _ in range(3)]
        self.assertEqual(readings[0], (500_000_000, 600_000_000, 100_000_000))
        for earlier, later in zip(readings, readings[1:]):
            self.assertEqual([b - a for a, b in zip(earlier, later)],
                             [30_000_000, 40_000_000, 10_000_000])


class DarwinCpuTwoThreadsTests(unittest.TestCase):
    """cpu_times is called from TWO THREADS, and it is not a pure function.

    The overlap is real and HostSampler's own docstring names it: at cold start
    `_do_state` builds on the REQUEST thread while `_refresh_loop` is building its first
    snapshot. HostSampler's lock guards `_prev` only - it calls the reader OUTSIDE that
    lock - so two calls into cpu_times can interleave, and cpu_times keeps state: the
    fetch and the unwrap are two steps over `_cpu_last` / `_cpu_laps`.

    THE INTERLEAVING, and why it is silent. Thread A fetches the OLDER ticks; thread B
    fetches the NEWER ones and unwraps first, which moves the baseline forward; A then
    unwraps its older reading against B's newer baseline. Every bucket reads smaller, and
    the unwrap cannot tell a wrap from any other backwards jump - by design, see its
    docstring - so it adds a lap to each, and the sampler is handed a delta about 2^32
    ticks wide. A-07 (a backwards delta) and A-08 (idle past the total) do not catch it:
    every number stays positive and idle stays well under the total, so what reaches the
    panel is a percentage nothing on the machine produced.
    """

    RAW = [(1_000, 1_000, 5_000, 0), (1_100, 1_100, 5_300, 0)]
    OLD_OUT = (500_000_000, 600_000_000, 100_000_000)
    NEW_OUT = (530_000_000, 640_000_000, 110_000_000)

    def test_an_older_reading_unwrapped_after_a_newer_one_invents_laps(self):
        """The mechanism, shown directly and without threads: this is what the two steps
        do when they run in the wrong order, and it is exactly right when the two
        readings really did arrive in that order (a wrap). The lock is what stops the
        order being an accident of scheduling."""
        platform = crabd.DarwinPlatform(load_info=lambda: None, clk_tck=100)
        platform._unwrap(list(self.RAW[1]))              # the newer reading lands first
        out = platform._unwrap(list(self.RAW[0]))        # ...and the older one follows
        # nice did not move, so it did not read backwards; the other three did.
        self.assertEqual(platform._cpu_laps, [1, 1, 1, 0])
        self.assertEqual(out[0], 1_000 + crabd.HOST_CPU_COUNTER_MODULUS)

    def test_a_reading_is_never_unwrapped_against_a_baseline_newer_than_itself(self):
        """Two threads, forced into that order: A is held INSIDE the reader holding the
        older ticks, B arrives with the newer ones. Serialised across both steps, A's
        reading is unwrapped against nothing (it is the first) and B's against A's - the
        order they were taken in - so no lap is invented and the gauge reads what the
        machine did."""
        first_in = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)             # never leave a thread parked
        counter = iter(range(len(self.RAW)))

        def load_info():
            index = next(counter)
            if index == 0:
                first_in.set()
                release.wait(5)
            return self.RAW[index]

        platform = crabd.DarwinPlatform(load_info=load_info, clk_tck=100)
        results = []
        threads = [threading.Thread(target=lambda: results.append(platform.cpu_times()))
                   for _ in range(2)]
        threads[0].start()
        self.assertTrue(first_in.wait(5), "the first reader should have been entered")
        threads[1].start()
        threads[1].join(timeout=0.2)             # serialised, it is still waiting here
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "a sampling thread did not finish")

        self.assertEqual(platform._cpu_laps, [0, 0, 0, 0])
        self.assertEqual(results, [self.OLD_OUT, self.NEW_OUT])
        # ...and the served number, which is the half a person would see.
        sampler = sampler_over(scripted(results))
        self.assertIsNone(sampler.sample()["cpuPct"])
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)


class LogOnceReset(unittest.TestCase):
    """`_log_once` is process-wide and these tests COUNT its lines, so the two host keys
    are cleared going in and the whole set is put back going out. Silence is the
    forbidden failure mode here, and a per-pass heartbeat is the other one."""

    def setUp(self):
        original = set(crabd._LOG_ONCE_SEEN)
        self.addCleanup(lambda: (crabd._LOG_ONCE_SEEN.clear(),
                                 crabd._LOG_ONCE_SEEN.update(original)))
        self.forget()

    @staticmethod
    def forget():
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_CPU_LOG_KEY)
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_MEM_LOG_KEY)

    @staticmethod
    def capture(call):
        """(what `call` returned, what it printed to stderr)."""
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            return call(), sys.stderr.getvalue()
        finally:
            sys.stderr = original


class DarwinClockGuardTests(LogOnceReset):
    """CLK_TCK turns mach's ticks into the sampler's 100 ns units, by INTEGER division.

    100 on every macOS measured, so the scale is 100_000 - but the division is where a
    surprising value stops being surprising and starts being silent: 0 divides by zero,
    a negative one inverts the counters, and one that does not divide 10_000_000 evenly
    drops part of every tick. All of them are refused, which is the contract's null
    column, and each says so once.
    """

    RAW = (100, 100, 300, 0)

    def test_a_clk_tck_that_cannot_scale_the_counters_serves_no_cpu(self):
        # 250 would be a bad example: it divides 10_000_000 exactly. 3 and 7 do not.
        for clk in (0, -1, 3, 7, True, "100", 100.0):
            with self.subTest(clk_tck=clk):
                self.forget()
                platform = crabd.DarwinPlatform(load_info=lambda: self.RAW,
                                                clk_tck=clk)
                out, noise = self.capture(
                    lambda: [platform.cpu_times() for _ in range(3)])
                self.assertEqual(out, [None, None, None])
                self.assertEqual(noise.count("cannot scale the CPU counters"), 1, noise)

    def test_the_sampler_then_serves_memory_with_no_cpu_beside_it(self):
        """Tier 2 of the three honest-failure tiers: one of the two reads failed, so
        that read's field is null and the other's are intact. Not a missing `host`
        block, which is what BOTH failing means."""
        platform = crabd.DarwinPlatform(load_info=lambda: self.RAW, clk_tck=7)
        block, _ = self.capture(sampler_over(platform.cpu_times).sample)
        self.assertEqual(block, {"cpuPct": None, "memPct": 62.5,
                                 "memUsedGB": 20.0, "memTotalGB": 32.0})

    def sysconf_counter(self, answers):
        """os.sysconf replaced by one that walks `answers` (a value, or an exception to
        raise) and records the names it was asked for."""
        asked = []
        queue = list(answers)
        missing = object()
        original = getattr(crabd.os, "sysconf", missing)

        def restore():
            if original is missing:
                del crabd.os.sysconf
            else:
                crabd.os.sysconf = original

        self.addCleanup(restore)

        def counting(name):
            asked.append(name)
            answer = queue.pop(0) if queue else 100
            if isinstance(answer, BaseException):
                raise answer
            return answer

        crabd.os.sysconf = counting
        return asked

    def test_the_clock_is_read_once_and_then_remembered(self):
        """CLK_TCK is a property of the KERNEL, not a reading that drifts, and this
        reader runs every two seconds for the life of the daemon. Resolving it per call
        buys nothing and pays a syscall for it."""
        asked = self.sysconf_counter([100])
        platform = crabd.DarwinPlatform(load_info=lambda: self.RAW)
        for _ in range(3):
            self.assertIsNotNone(platform.cpu_times())
        self.assertEqual(asked, ["SC_CLK_TCK"])

    def test_a_read_that_failed_is_not_what_gets_remembered(self):
        """Only an ANSWER is cached. A sysconf that raised, or one that answered
        something that cannot scale the counters, has told this reader nothing - and a
        remembered failure would be a gauge that stays dark for the life of the process
        over one bad call."""
        asked = self.sysconf_counter([OSError("no sysconf yet"), 7, 100])
        platform = crabd.DarwinPlatform(load_info=lambda: self.RAW)
        out, _noise = self.capture(
            lambda: [platform.cpu_times() for _ in range(3)])
        self.assertEqual(out[:2], [None, None])          # raised, then unusable
        self.assertEqual(out[2], (30_000_000, 40_000_000, 10_000_000))
        self.assertEqual(asked, ["SC_CLK_TCK"] * 3)
        # ...and once it has an answer, that is the end of the asking.
        self.assertIsNotNone(platform.cpu_times())
        self.assertEqual(len(asked), 3)

    def test_a_reading_that_is_not_four_numbers_serves_no_cpu_and_says_so_once(self):
        """Two shapes, one answer. `host_statistics` writing a different number of words
        and a reply whose members are not numbers are the same failure from here: the
        four buckets this arithmetic names are not where it thinks they are, so there is
        nothing to scale and the honest answer is no cpuPct at all."""
        for raw in ((100, 100, 300), (100, 100, 300, 0, 0), ("a", "b", "c", "d"),
                    (None, None, None, None), 4):
            with self.subTest(reading=raw):
                self.forget()
                platform = crabd.DarwinPlatform(load_info=lambda: raw, clk_tck=100)
                out, noise = self.capture(
                    lambda: [platform.cpu_times() for _ in range(3)])
                self.assertEqual(out, [None, None, None])
                self.assertEqual(noise.count("not four numbers"), 1, noise)

    def test_a_host_with_no_sc_clk_tck_answers_absence_not_an_exception(self):
        """Windows is such a host - `os.sysconf` is not there at all. DarwinPlatform is
        never SELECTED on one, but the seam lets anything build one, and a reader called
        from a daemon thread has to answer None rather than raise."""
        missing = object()
        original = getattr(crabd.os, "sysconf", missing)

        def restore():
            if original is missing:
                del crabd.os.sysconf
            else:
                crabd.os.sysconf = original

        self.addCleanup(restore)

        def boom(_name):
            raise OSError("no sysconf here")

        crabd.os.sysconf = boom
        platform = crabd.DarwinPlatform(load_info=lambda: self.RAW)
        out, noise = self.capture(
            lambda: [platform.cpu_times() for _ in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(noise.count("SC_CLK_TCK unreadable (OSError)"), 1, noise)


# ------------------------------------------------------------------------- memory

#: MEASURED 2026-09-04 on a 128 GiB Apple-silicon Mac, page size 16384. The counts here
#: are the measured GiB figures turned back into pages, so every number below is one a
#: real machine produced: free 28.67, active 48.19, inactive 44.70, wired 5.54,
#: speculative 4.03, compressor 0.00, app (internal - purgeable) 60.42.
MEASURED_PAGES = {
    "count": 38,
    "free_count": 1_879_000,
    "active_count": 3_158_000,
    "inactive_count": 2_929_000,
    "wire_count": 363_000,
    "purgeable_count": 0,
    "speculative_count": 264_000,
    "compressor_page_count": 0,
    "external_page_count": 2_245_000,
    "internal_page_count": 3_960_000,
}
MEASURED_SYSCTL = {"hw.memsize": 137_438_953_472, "vm.pagesize": 16384}
#: (internal - purgeable + wire + compressor) x page size, from the counts above.
MEASURED_USED = 4_323_000 * 16384


def darwin_memory(pages=None, sysctl=None):
    """A DarwinPlatform whose memory seams answer the measured Mac, with overrides."""
    values = dict(MEASURED_SYSCTL, **(sysctl or {}))
    counts = dict(MEASURED_PAGES, **(pages or {}))
    return crabd.DarwinPlatform(vm_stats=lambda: counts,
                                sysctl=lambda name: values[name])


class DarwinMemoryFormulaTests(LogOnceReset):
    """`used` is ACTIVITY MONITOR's "Memory Used": app memory + wired + compressed,
    which is (internal_page_count - purgeable_count) + wire_count +
    compressor_page_count, in pages. The contract's promise for this row has always
    been that it matches what the OS's own monitor shows, and on a Mac there are two
    other plausible answers that do not: `top`'s used (total - free) is 99.3 GiB from the
    page counts recorded above - `top` itself printed the rounded "98G" - and
    free + inactive + speculative counted as available reads differently again. Neither
    is what the user sees when they look.
    """

    def test_the_reading_is_total_and_what_is_left_after_activity_monitors_used(self):
        self.assertEqual(darwin_memory().memory(),
                         (137_438_953_472, 137_438_953_472 - MEASURED_USED))

    def test_the_served_block_carries_the_measured_numbers(self):
        sampler = crabd.HostSampler(times=lambda: None,
                                    memory=darwin_memory().memory,
                                    platform=crabd.NullPlatform())
        block = sampler.sample()
        self.assertEqual(block["memTotalGB"], 128.0)
        self.assertEqual(block["memUsedGB"], 66.0)
        self.assertEqual(block["memPct"], 51.5)
        # The plausible wrong answer, named: `top`'s used is total - free, which from the
        # page counts above is 106_653_417_472 bytes - 99.3 GiB, half the machine again.
        # (`top` printed "98G" for it; the two agree, one of them rounded.)
        self.assertNotEqual(block["memUsedGB"], 99.3)


class DarwinMemoryRefusalTests(LogOnceReset):
    """Five ways the memory read can be unusable, all of them answered with None and
    one stderr line - never a fabricated figure. Each has a specific wrong answer it is
    there to prevent, named in its own test."""

    def refuses(self, pages=None, sysctl=None):
        self.forget()
        out, noise = self.capture(darwin_memory(pages, sysctl).memory)
        self.assertIsNone(out)
        self.assertEqual(noise.count("serving no host memory"), 1, noise)

    def test_a_reply_whose_word_count_is_not_the_struct_we_declared_is_refused(self):
        """`count` comes back saying how many 32-bit words the kernel wrote. A different
        number means a different struct layout, so purgeable_count and the rest are not
        at the offsets this code reads them from - and the arithmetic would then be
        confident nonsense rather than an error."""
        for count in (0, 37, 39, 44):
            with self.subTest(count=count):
                self.refuses(pages={"count": count})

    def test_a_page_size_that_is_not_a_positive_power_of_two_is_refused(self):
        """0 would multiply every page count to nothing; 12345 is not a page size any
        machine has and would scale the whole reading by a wrong constant.

        `True` is the sharp one, and it is why `_positive_int` refuses bools: it IS an
        int in Python, it is positive, and 1 is a power of two - so it passes every check
        in this line and then scales the whole reading by 1 byte per page instead of
        16384. A machine using 66 GiB would be reported as using 4 MB.
        """
        for page in (0, -16384, 12345, None, True):
            with self.subTest(page=page):
                self.refuses(sysctl={"vm.pagesize": page})

    def test_an_unreadable_installed_size_is_refused(self):
        """Zero installed memory is not a machine, and it is also the denominator of
        memPct: divided into, it is a ZeroDivisionError inside a daemon thread. `True`
        is an installed size of one byte, which every used figure exceeds."""
        for total in (0, -1, None, True):
            with self.subTest(total=total):
                self.refuses(sysctl={"hw.memsize": total})

    def test_used_larger_than_installed_is_refused_rather_than_served_negative(self):
        """available = total - used, so a used past total is a NEGATIVE availability.
        HostSampler clamps that back to a plausible-looking figure rather than
        rejecting it, so the refusal has to happen here."""
        self.refuses(pages={"internal_page_count": 10_000_000})

    def test_a_used_below_zero_is_refused_rather_than_served_as_empty(self):
        """The other end of the same bound, and it is on the TOTAL, not on any one term.

        The check is `0 <= used <= total`, so a purgeable count a few pages past
        internal_page_count is tolerated by design - the four counts are sampled by the
        kernel at slightly different moments and a little skew is not a bad reading. What
        is refused is the SUM coming out negative: here purgeable exceeds internal by
        more than wire_count can make up, and HostSampler would serve memPct 0.0 for it -
        a machine reported as using none of its memory, which is the fabricated zero the
        contract forbids.
        """
        self.refuses(pages={"purgeable_count": 5_000_000})

    def test_a_failed_syscall_is_refused(self):
        """host_statistics64 answering non-zero comes back as None from the seam."""
        self.forget()
        platform = crabd.DarwinPlatform(vm_stats=lambda: None,
                                        sysctl=lambda name: MEASURED_SYSCTL[name])
        out, noise = self.capture(platform.memory)
        self.assertIsNone(out)
        self.assertEqual(noise.count("serving no host memory"), 1, noise)

    def test_a_raising_seam_is_refused_and_named_once(self):
        """sysctlbyname is a syscall; OSError out of it is a real shape, and so is a
        KeyError from a reply that is missing a field this code reads. Both are one
        stderr line for the life of the process, not one every two seconds."""
        def boom(_name):
            raise OSError("sysctl exploded")

        working = {"sysctl": lambda name: MEASURED_SYSCTL[name],
                   "vm_stats": lambda: dict(MEASURED_PAGES)}
        for seam in ({"sysctl": boom},
                     {"vm_stats": lambda: {"count": 38}}):     # no page counts at all
            with self.subTest(seam=sorted(seam)):
                self.forget()
                platform = crabd.DarwinPlatform(**{**working, **seam})
                out, noise = self.capture(
                    lambda: [platform.memory() for _ in range(3)])
                self.assertEqual(out, [None, None, None])
                self.assertEqual(noise.count("serving no host memory"), 1, noise)

    def test_the_sampler_serves_cpu_with_no_memory_beside_it(self):
        """The other half of tier 2: memory failed, so its three fields are null and
        cpuPct is intact. Still a `host` block - a missing block is what BOTH failing
        means."""
        self.forget()
        raw = [(1_000, 1_000, 5_000, 0), (1_100, 1_100, 5_300, 0)]
        cpu = crabd.DarwinPlatform(load_info=scripted(raw), clk_tck=100)
        sampler = crabd.HostSampler(
            times=cpu.cpu_times, memory=darwin_memory({"count": 39}).memory,
            platform=crabd.NullPlatform())
        block, _ = self.capture(lambda: [sampler.sample(), sampler.sample()][1])
        self.assertEqual(block, {"cpuPct": 40.0, "memPct": None,
                                 "memUsedGB": None, "memTotalGB": None})


class FakeEntryPoint:
    """A ctypes entry point that records what was declared on it and what it was called
    with. `argtypes` and `restype` start as ctypes leaves them on a fresh function
    pointer: unset."""

    def __init__(self, name):
        self.name = name
        self.argtypes = None
        self.restype = None
        self.answer = 0
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.answer


class FakeLibc:
    """A stand-in for the loaded libSystem: attribute access mints an entry point."""

    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, FakeEntryPoint(name))


class DarwinLibcDeclarationTests(unittest.TestCase):
    """Every mach entry point crabd calls declares its argument and return types.

    ctypes defaults BOTH to `c_int`, and that is the trap here rather than a formality:
    `mach_port_t` is an unsigned 32-bit value, so a host port at or above 2^31 does not
    fit the default c_int ARGUMENT conversion on the way in to host_statistics - which is
    where the failure would happen, and it would happen on some machines and not others.
    The return declaration is the same fact one step later.
    """

    NAMES = ("mach_host_self", "host_statistics", "host_statistics64", "sysctlbyname")

    def loaded(self) -> FakeLibc:
        """crabd's libc holder, freshly resolved over a fake CDLL. Pure: nothing here
        loads a real library, so it runs on any OS."""
        fake = FakeLibc()
        original = (crabd._DARWIN_LIBC, crabd.ctypes.CDLL, crabd.ctypes.util.find_library)

        def restore():
            (crabd._DARWIN_LIBC, crabd.ctypes.CDLL,
             crabd.ctypes.util.find_library) = original

        self.addCleanup(restore)
        crabd._DARWIN_LIBC = None
        crabd.ctypes.util.find_library = lambda _name: "libc.dylib"
        crabd.ctypes.CDLL = lambda *a, **k: fake
        return crabd._darwin_libc()

    def test_all_four_entry_points_declare_their_types(self):
        libc = self.loaded()
        for name in self.NAMES:
            with self.subTest(entry_point=name):
                entry = getattr(libc, name)
                self.assertIsNotNone(entry.argtypes, name)
                self.assertIsNotNone(entry.restype, name)

    def test_the_host_port_is_unsigned_going_in_and_coming_out(self):
        """The one that matters. Undeclared, a port with the high bit set is converted
        as a signed c_int in both directions."""
        libc = self.loaded()
        self.assertEqual(libc.mach_host_self.restype, crabd.ctypes.c_uint32)
        for name in ("host_statistics", "host_statistics64"):
            with self.subTest(entry_point=name):
                self.assertEqual(getattr(libc, name).argtypes[0], crabd.ctypes.c_uint32)


class DarwinLibcIsLookedUpOnceTests(LogOnceReset):
    """A host where libSystem is not there answers that ONCE.

    `find_library` is a dyld search - a subprocess on some platforms and a filesystem
    walk on others - and this reader runs every two seconds. The success path already
    resolved once; the FAILURE path did not, so a crabd whose platform was forced to
    Darwin on a machine that has no libSystem re-ran the whole search twice a pass, for
    ever, to be told the same thing.
    """

    def rig(self):
        """find_library and CDLL replaced; returns the list of find_library calls."""
        asked = []
        original = (crabd._DARWIN_LIBC, crabd._DARWIN_HOST_PORT,
                    crabd.ctypes.CDLL, crabd.ctypes.util.find_library)

        def restore():
            (crabd._DARWIN_LIBC, crabd._DARWIN_HOST_PORT, crabd.ctypes.CDLL,
             crabd.ctypes.util.find_library) = original

        self.addCleanup(restore)
        crabd._DARWIN_LIBC = None
        crabd._DARWIN_HOST_PORT = None

        def searching(name):
            asked.append(name)
            return None

        def refusing(*_a, **_k):
            raise OSError("no libSystem on this host")

        crabd.ctypes.util.find_library = searching
        crabd.ctypes.CDLL = refusing
        return asked

    def test_three_samples_search_for_the_library_once(self):
        asked = self.rig()
        platform = crabd.DarwinPlatform(clk_tck=100)
        out, noise = self.capture(lambda: [platform.cpu_times() for _ in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(asked, ["c"])
        self.assertEqual(noise.count("serving no host CPU"), 1, noise)

    def test_the_memory_reader_is_answered_by_the_same_remembered_failure(self):
        asked = self.rig()
        platform = crabd.DarwinPlatform()
        out, _noise = self.capture(lambda: [platform.memory() for _ in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(asked, ["c"])


class DarwinHostPortTests(unittest.TestCase):
    """`mach_host_self()` is asked ONCE, not once per reading.

    It takes a SEND RIGHT and hands back a reference, and nothing here ever releases it
    (`mach_port_deallocate`), so every call adds one uref to this task's right to the
    host port. MEASURED 2026-09-04 on this Mac: 2 urefs after one call, 1002 after 1001 -
    one leaked reference per call, exactly.

    The endpoint is undramatic (at two calls per two-second pass it is years to
    MACH_PORT_UREFS_MAX, and past it host_statistics simply fails and the gauges null out
    honestly), which is why this is a tidiness fix and not an incident. It is still a
    counter climbing for the life of a daemon meant to run for months, over a value that
    cannot change: the host port is a property of the task.
    """

    def use_fake_libc(self) -> FakeLibc:
        fake = FakeLibc()
        original = (crabd._DARWIN_LIBC, crabd._DARWIN_HOST_PORT)

        def restore():
            crabd._DARWIN_LIBC, crabd._DARWIN_HOST_PORT = original

        self.addCleanup(restore)
        crabd._DARWIN_LIBC = fake
        crabd._DARWIN_HOST_PORT = None
        # A port with the HIGH BIT SET, which is the one a signed conversion mangles.
        fake.mach_host_self.answer = 0x9000_0001
        fake.host_statistics.answer = 0
        fake.host_statistics64.answer = 0
        return fake

    def test_three_readings_resolve_the_port_once_and_all_pass_the_same_one(self):
        fake = self.use_fake_libc()
        crabd._darwin_cpu_load_info()
        crabd._darwin_cpu_load_info()
        crabd._darwin_vm_statistics64()
        self.assertEqual(len(fake.mach_host_self.calls), 1)
        passed = [call[0] for call in
                  fake.host_statistics.calls + fake.host_statistics64.calls]
        self.assertEqual(passed, [0x9000_0001] * 3)


class DarwinVmStructTests(unittest.TestCase):
    """The declared struct is 38 32-bit words, which is both what the call passes in as
    its capacity and what it checks on the way back. Pinned here because the two halves
    are far apart in the file and a field added to one without the other would make the
    capacity and the drift check disagree."""

    def test_the_declared_struct_is_thirty_eight_words(self):
        self.assertEqual(crabd.ctypes.sizeof(crabd._VM_STATISTICS64) % 4, 0)
        self.assertEqual(crabd.ctypes.sizeof(crabd._VM_STATISTICS64) // 4, 38)

    def test_every_field_the_formula_reads_is_declared(self):
        declared = {name for name, _type in crabd._VM_STATISTICS64._fields_}
        self.assertLessEqual(set(crabd.HOST_VM_FIELDS), declared)


# ------------------------------------------------- what this Mac actually serves

class StubLimits:
    def get(self, now, force=False):
        return {"available": False, "note": "stub", "fiveHour": None, "weekly": None,
                "extra": [], "subscriptionType": None, "rateLimitTier": None}


@unittest.skipUnless(sys.platform == "darwin",
                     "mach host_statistics / host_statistics64 / sysctlbyname")
class DarwinHostLiveReadTests(unittest.TestCase):
    """A read-only measurement of THIS Mac, bounds-checked. Everything above proves the
    arithmetic against numbers this file invented; this is the one place it meets a real
    kernel, and it is the healthy-night check for the whole reader."""

    def sampler(self):
        return crabd.HostSampler(platform=crabd.DarwinPlatform())

    def test_the_real_counters_produce_a_sane_block(self):
        sampler = self.sampler()
        block = sampler.sample()
        self.assertIsNotNone(block, "the mach counters should be readable here")
        self.assertIsNone(block["cpuPct"])          # first sample, by construction
        self.assertGreater(block["memTotalGB"], 0.5)
        self.assertGreaterEqual(block["memPct"], 0.0)
        self.assertLessEqual(block["memPct"], 100.0)
        self.assertLessEqual(block["memUsedGB"], block["memTotalGB"])

        # Burn a little CPU so the counters definitely move past CPU_MIN_TOTAL_TICKS,
        # then take the second sample the delta needs. Retried rather than slept on: a
        # fixed sleep would be either flaky or slow.
        cpu = None
        for _ in range(20):
            deadline = time.perf_counter() + 0.05
            while time.perf_counter() < deadline:
                pass
            cpu = sampler.sample()["cpuPct"]
            if cpu is not None:
                break
        self.assertIsNotNone(cpu, "two samples 50 ms+ apart should yield a percentage")
        self.assertIsInstance(cpu, float)
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)

    def test_the_installed_size_agrees_with_the_sysctl_command(self):
        """A SECOND OPINION, not the same number twice: `sysctl -n hw.memsize` is a
        different code path to the same fact, so this catches a reader that is
        self-consistently wrong (a mis-sized buffer, the low half of a 64-bit value)."""
        reported = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                                  capture_output=True, text=True, timeout=10,
                                  check=True).stdout.strip()
        self.assertEqual(self.sampler().sample()["memTotalGB"],
                         round(int(reported) / 1024 ** 3, 1))


@unittest.skipUnless(sys.platform == "darwin", "what a default crabd serves on a Mac")
class DarwinDefaultBuilderTests(unittest.TestCase):
    """The phase's behaviour claim, taken off a real document.

    The widget feature-detects `host` by PRESENCE, so a Mac gaining the block is the
    whole user-visible change: until now a default builder here served no `host` key at
    all and the panel rendered nothing where the gauges go.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "projects").mkdir()
        original = crabd.USER_CONFIG_FILE
        crabd.USER_CONFIG_FILE = root / "config.json"
        self.addCleanup(lambda: setattr(crabd, "USER_CONFIG_FILE", original))
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(root / "projects"), crabd.HookTracker(),
            StubLimits(), 0.0)

    def test_the_first_build_carries_memory_and_a_null_cpu(self):
        state = self.builder.build()
        self.assertIn("host", state)
        self.assertIsNone(state["host"]["cpuPct"])      # no predecessor reading yet
        self.assertIsInstance(state["host"]["memTotalGB"], float)
        self.assertGreater(state["host"]["memTotalGB"], 0.5)
        self.assertEqual(state["schema"], 5)            # nothing else moved

    def test_the_block_survives_json(self):
        """dump_state refuses non-finite floats and takes the WHOLE document down the
        sanitising path when it meets one, so a live reading is checked through it."""
        parsed = json.loads(crabd.dump_state(self.builder.build()))
        self.assertGreater(parsed["host"]["memTotalGB"], 0.5)


if __name__ == "__main__":
    unittest.main()
