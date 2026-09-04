"""A keep-alive HTTP client for the crabd test suites.

WHY THIS EXISTS - measured 2026-08-26, and it is the whole reason the suite could not
reproduce its own result.

Every socket-based test used `urllib.request.urlopen`, which opens a FRESH TCP
connection per request and closes it. On Windows that is expensive in a way that has
nothing to do with speed: ephemeral ports are handed out from a single SEQUENTIAL
cursor spanning 1024-65535, and the side that closes first holds its port in TIME_WAIT.
Run a few thousand short connections and the cursor wraps into 4-tuples that are still
in TIME_WAIT; the kernel silently DROPS the SYN, the client retransmits (~0.5 s, 1.5 s,
3 s, 6 s...), and a request that normally takes 1 ms takes six seconds or times out.

That is exactly the failure the suite kept producing: `TimeoutError` inside
`sock.connect()`, on a different test every run, at a rate of roughly one per few
hundred connections. Not a defect in crabd, and not fixable by retrying the test.

Measured, three variants, same work (80 servers, 320 requests):

    new server per iter + new connection per request : 12 stalls   101.3 s
    new server per iter + ONE keep-alive connection  :  0 stalls    41.1 s
    ONE server          + ONE keep-alive connection  :  0 stalls     0.7 s

So the lever is the CONNECTION COUNT, not the server count - churning 80 servers with
keep-alive was perfectly clean. Reusing the connection is also what every real client of
crabd does: the widget polls, the status line command posts on a debounce, and crabd has
advertised HTTP/1.1 keep-alive since v0.1.

One connection per THREAD, because the permission long-poll tests deliberately fire
concurrent requests and a single http.client connection is not shareable.

THE RESIDUAL, and what it actually is
-------------------------------------
Keep-alive cuts the exposure by ~5x but does not remove it, because the rest is not
OURS. Caught in the act on 2026-08-26 - netstat taken at the instant a connect timed
out, against a server whose own process was idle:

    port LISTENING  = True                     threads_alive = 2
    TCP  127.0.0.1:54560   127.0.0.1:1151    SYN_RECEIVED
    TCP  127.0.0.1:54560   127.0.0.1:56570   SYN_RECEIVED
    TCP  127.0.0.1:54560   127.0.0.1:56579   SYN_RECEIVED
    TCP  127.0.0.1:54560   127.0.0.1:58057   SYN_RECEIVED

The listening socket is healthy, the accept backlog is 128, the process has two threads
and is doing nothing - and four separate connections are stuck half-open because the
SYN-ACK never completes. On LOOPBACK. Nothing in Python can make a dropped SYN-ACK
arrive; the application is not a participant in a handshake that never finishes.

It reproduces with urllib and with http.client, with the accept backlog at 5 and at 128,
inside unittest and in a bare loop with no test framework at all, against a server
churned every iteration and against one that listened untouched for the whole run. It
arrives in WINDOWS lasting seconds to tens of seconds and clears on its own. On this
workstation the TCP dynamic port range is set to 1024-65535 rather than the Windows
default 49152-65535, so both ends draw ports from the whole registered range where
filter drivers hook - an endpoint security product inspecting new loopback flows is the
usual cause of exactly this signature.

Two things follow, and the harness does both:

  - `start_test_server()` below PROVES the port before a test runs, and re-binds
    somewhere else if it cannot. A poisoned port is then a fixture problem the fixture
    solves, not a failure attributed to whatever test drew it.
  - `connect()` - and ONLY connect() - is retried.

The retry is not a retry-until-green, and the distinction is the whole point: a SYN that
never gets a SYN-ACK never reaches the application, so crabd is not a participant in
what is being retried. The request, its status, its body and every timing assertion stay
exactly as strict as they were - `RETRYABLE` deliberately excludes TimeoutError, so a
request that hangs AFTER connecting still fails the test, which is the failure mode this
whole file exists to expose. Both mitigations are counted (`connect_retries`,
`ABANDONED_PORTS`) and printed at exit, so they are auditable rather than invisible, and
SIDECRAB_TEST_NO_CONNECT_RETRY=1 turns the retry off for anyone who wants to watch the
host misbehave directly.

Rejected alternative, recorded so it is not re-tried: closing the client socket with
SO_LINGER(1, 0) to avoid TIME_WAIT entirely. It does avoid TIME_WAIT, and it also sends
an RST that lands mid-handler and fills the server's stderr with ConnectionResetError
tracebacks. Measured 2026-08-26; not worth it.
"""

import atexit
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crabd  # noqa: E402


class Reply:
    """Enough of the urllib response surface for the call sites that used it -
    `.status`, `.headers`, and the body already read."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def read(self):
        return self.body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class KeepAliveClient:
    """127.0.0.1:<port>, one reused connection per thread."""

    # The retryable set is exactly "the server closed an idle keep-alive connection",
    # which is normal HTTP/1.1 and not a flake. Deliberately NOT TimeoutError: a request
    # that timed out is the thing these tests exist to catch, and retrying it would hide
    # the very failure mode this file was written to expose.
    RETRYABLE = (http.client.RemoteDisconnected, http.client.BadStatusLine,
                 http.client.CannotSendRequest, http.client.ResponseNotReady,
                 ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    # Establishment only. Short per-attempt timeout because a healthy loopback connect
    # is sub-millisecond - anything past this is the host transient, and waiting the
    # full request timeout for it only makes the run slower, not more correct.
    CONNECT_ATTEMPT_SEC = 2.0
    CONNECT_ATTEMPTS = 5

    #: Every connect that had to be retried, across all clients. Non-zero is not a test
    #: failure - it is this host stalling loopback SYNs - but it is the number to look
    #: at before believing any story about crabd being slow.
    connect_retries = 0
    _class_lock = threading.Lock()

    def __init__(self, port: int, timeout: float = 15.0):
        self.port = port
        self.timeout = timeout
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

    @classmethod
    def _note_retry(cls):
        with cls._class_lock:
            cls.connect_retries += 1

    def _connect(self):
        """A new connection, retrying ONLY the TCP handshake. See the module docstring
        for why this is not a retry-until-green."""
        attempts = 1 if os.environ.get("SIDECRAB_TEST_NO_CONNECT_RETRY") \
            else self.CONNECT_ATTEMPTS
        last = None
        for attempt in range(attempts):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.port,
                timeout=self.CONNECT_ATTEMPT_SEC if attempts > 1 else self.timeout)
            try:
                conn.connect()
            except (TimeoutError, ConnectionRefusedError, OSError) as exc:
                last = exc
                try:
                    conn.close()
                except Exception:       # noqa: BLE001
                    pass
                if attempt + 1 < attempts:
                    self._note_retry()
                    time.sleep(0.05 * (attempt + 1))
                continue
            # Connected: restore the caller's real timeout for the exchange itself.
            conn.sock.settimeout(self.timeout)
            conn.timeout = self.timeout
            return conn
        raise last

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def _drop(self):
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:       # noqa: BLE001 - teardown of a dead socket
                pass

    def request(self, method, path, body=None, headers=None, timeout=None) -> Reply:
        last = None
        for attempt in (0, 1):
            try:
                conn = self._connection()
                if timeout is not None and conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                reply = Reply(response.status, response.headers, response.read())
                if response.will_close:
                    # crabd closes the connection deliberately after an over-cap body -
                    # honour it rather than reusing a socket it has finished with.
                    self._drop()
                return reply
            except self.RETRYABLE as exc:
                last = exc
                self._drop()
                if attempt:
                    raise
        raise last                                          # pragma: no cover

    def get(self, path, headers=None, timeout=None) -> Reply:
        # `headers` exists for the SEC-4 read-gate tests (v0.16.0): the Origin gate now
        # applies to GET, so a read has to be able to carry one.
        return self.request("GET", path, headers=headers, timeout=timeout)

    def post(self, path, body=b"", headers=None, timeout=None) -> Reply:
        head = {"Content-Type": "application/json"}
        head.update(headers or {})
        return self.request("POST", path, body=body, headers=head, timeout=timeout)

    def close(self):
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:       # noqa: BLE001
                pass
        self._local = threading.local()


#: Ports that had to be abandoned because nothing could reach them. Reported by the
#: suite's tearDownModule hooks; a non-zero count is the host misbehaving, not crabd.
ABANDONED_PORTS = []


def start_test_server(server_factory, attempts: int = 4):
    """-> (server, thread, port, client), on a port PROVEN to be reachable.

    Binds, serves, and then makes one real request before handing the fixture back. If
    that request cannot get through, the port is abandoned and another is drawn.

    This is a fixture-establishment step, not a test retry. The netstat capture in the
    module docstring is why it exists: a listening socket on this host can be
    unreachable through no fault of the process behind it, and a test that draws such a
    port would otherwise fail for a reason that has nothing to do with what it asserts.
    The proving request is also the client's FIRST request, so the connection every
    later assertion rides on is one that has already been shown to work.
    """
    last = None
    for attempt in range(attempts):
        server = server_factory()
        port = server.server_address[1]
        # The CONSTANT, never the literal: the production port moved once already, and a
        # guard that names a number a suite ago stops guarding the day it moves again.
        assert port != crabd.DEFAULT_PORT, \
            "test server must never bind the production port"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = KeepAliveClient(port)
        try:
            client.get("/v1/health", timeout=3)
            return server, thread, port, client
        except (TimeoutError, OSError) as exc:
            last = exc
            ABANDONED_PORTS.append(port)
            client.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            time.sleep(0.1 * (attempt + 1))
    raise AssertionError(
        f"could not reach a test server on {attempts} different ports - the host is "
        f"dropping loopback SYN-ACKs (see this module's docstring); last error: {last!r}")


@atexit.register
def _report_host_interference():
    """One line, only when it happened. The mitigations above are deliberately loud:
    a suite that silently papers over a host problem is how the problem reaches
    production unnoticed - and crabd's real hooks ride the same loopback."""
    retries = KeepAliveClient.connect_retries
    if not retries and not ABANDONED_PORTS:
        return
    print(f"\n[loopback] host dropped SYN-ACKs during this run: "
          f"{retries} connect retries, {len(ABANDONED_PORTS)} port(s) abandoned "
          f"({', '.join(str(p) for p in ABANDONED_PORTS[:8])}). "
          f"See companion/tests/_httpkeepalive.py - this is the workstation's TCP "
          f"stack, not crabd.", file=sys.stderr)


def settle(predicate, timeout: float = 5.0, what: str = "the side effect"):
    """Wait for a FIRE-AND-FORGET endpoint's side effect to land. -> the predicate's
    value, or raises AssertionError on timeout.

    /v1/hook, /v1/statusline, /v1/metrics and /v1/logs all answer BEFORE they parse -
    deliberately, because the producer is a hook or a status line command inside the
    session the operator is working in, and none of them may be made to wait on crabd.
    So the 204 landing does NOT mean the document has been ingested, and a test that
    asserts the side effect on the next line is asserting a race.

    Those assertions used to pass because `urlopen` opened and tore down a TCP
    connection per request, and the teardown happened to give the handler thread enough
    time. Keep-alive removed that accidental delay and the race became a deterministic
    failure - which is the honest outcome: the test was always wrong, the old transport
    was hiding it.

    This is a barrier, not a retry. It waits for something crabd has been TOLD to do and
    fails loudly if it never happens; it does not re-run a request or re-assert a value.
    """
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() >= deadline:
            raise AssertionError(
                f"{what} never landed within {timeout}s - the endpoint answers before "
                f"it parses, so this is a real failure, not a slow machine")
        time.sleep(0.01)


def quiesce(counter, expected: int, timeout: float = 5.0):
    """Wait until a receiver has SEEN `expected` documents, for tests that assert
    something did NOT happen. Without it those tests can pass because the body had not
    been looked at yet, which is a false pass rather than a flake."""
    settle(lambda: counter() >= expected, timeout=timeout,
           what=f"{expected} document(s) reaching the receiver")
