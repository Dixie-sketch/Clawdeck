"""The two transport gates in front of the panel: which ORIGIN may talk to crabd, and
the header every POST has to carry.

Until 0.31.0 the origin rule was one predicate - a present http(s) Origin is a visited
web page, refuse it - and that was right while the panel was an iCUE widget served from
disk, whose own Origin serialises to `null`. crabd now serves the panel itself, so the
panel HAS a real web origin (`http://localhost:<port>`), and "refuse every http origin"
would refuse the product. The allowlist is the case the old docstring anticipated: a
panel build with a stable origin, allowlisted to that exact value.

The allowlist alone would not be enough. `null` still has to be allowed - the iCUE build
legitimately sends it and a sandboxed iframe can forge it - so a forged-null page would
inherit every write path. The header gate is what closes that: a custom request header
turns a CORS-simple POST into a preflighted one, and the preflight only unlocks
`X-SideCrab-Panel` for a panel origin or a non-web scheme, never for `null`. The two
gates are therefore proven SEPARATELY here; either one alone leaves a hole.
"""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import KeepAliveClient, start_test_server  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation the other companion modules take: these globals name real
    files under ~, and the live limits cache was poisoned with fixture data exactly this
    way on 2026-08-26."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.PANEL_TOKEN_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.PANEL_TOKEN_FILE = root / "panel-token"
    # The Keychain kill switch, for the same reason as the paths above: with it
    # False, nothing in this module can reach the operator's login Keychain - no
    # prompt on their desktop, and no secret this suite has any business seeing.
    setUpModule.keychain = crabd.KEYCHAIN_CREDENTIALS_ENABLED
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = False


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.PANEL_TOKEN_FILE) = setUpModule.originals
    # The fixtures leave a builder on the Handler CLASS pointing into a
    # TemporaryDirectory that is about to be deleted.
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = setUpModule.keychain


class StubLimits:
    def get(self, now, force=False):
        return {"available": False, "note": "stub", "fiveHour": None, "weekly": None,
                "extra": [], "subscriptionType": None, "rateLimitTier": None}


ACAO = "Access-Control-Allow-Origin"
ACAH = "Access-Control-Allow-Headers"
JSON = "application/json"
HEADER = crabd.PANEL_HEADER


class PanelServed(unittest.TestCase):
    """A real crabd on a bound ephemeral port, with one session WAITING on the operator.

    The waiting session is the observable side effect: `ack-all` is the whole-panel
    gesture with no session id and no pairing code, so it is the cheapest write to aim a
    gate test at - and `sessions[].acked` says whether a refusal actually refused, or
    merely returned a 403 after doing the work.
    """

    SID = "9a9a9a9a-0000-0000-0000-00000000000d"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        projects = root / "projects"
        projects.mkdir(parents=True)
        self.config_path = root / "config.json"
        self.hooks = crabd.HookTracker()
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(projects), self.hooks, StubLimits(), time.time(),
            crabd.UserConfig(self.config_path))
        self.builder.panel_token = crabd.PanelToken(None, "K7QXM2PDAB")
        crabd.Handler.builder = self.builder
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.addCleanup(self.client.close)
        self.addCleanup(self._stop)
        self.hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                           "cwd": "/Users/x/dev/sidecrab", "message": "waiting on you"})
        self.rebuild()

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    # -- helpers

    def rebuild(self):
        state = self.builder.build()
        with self.builder._lock:
            self.builder._state = state
        return state

    def row(self):
        return next(r for r in self.rebuild()["sessions"] if r["id"] == self.SID)

    def acked(self) -> bool:
        return self.row()["acked"]

    def panel_origin(self, port=None) -> str:
        return f"http://localhost:{port or self.port}"

    def get_state(self, origin=None):
        headers = {} if origin is None else {"Origin": origin}
        return self.client.get("/v1/state", headers=headers)

    def post_ack_all(self, origin=None, header="1"):
        """POST /v1/action ack-all. Sent through `request`, not `post`, because the
        harness's post() adds the panel header the way every real client does - and the
        header is one of the two things under test here."""
        headers = {"Content-Type": JSON}
        if header is not None:
            headers[HEADER] = header
        if origin is not None:
            headers["Origin"] = origin
        return self.client.request("POST", "/v1/action",
                                   body=b'{"action":"ack-all"}', headers=headers)


# ------------------------------------------------------ B: the origin allowlist

class OriginAllowlistTests(PanelServed):
    """The matrix, asserted for a READ and a WRITE alike - the two are gated by the same
    predicate and a rule that only holds on one of them is not a rule."""

    def both(self, origin=None):
        """-> (GET /v1/state, POST /v1/action). The POST carries the panel header so
        that only the ORIGIN gate is under test on this row."""
        return self.get_state(origin), self.post_ack_all(origin)

    def assertRefused(self, origin):
        for reply in self.both(origin):
            self.assertEqual(reply.status, 403, origin)
            self.assertEqual(reply.body, crabd.CROSS_SITE_REFUSED, origin)
            self.assertIsNone(reply.headers.get(ACAO), origin)

    def assertServed(self, origin, acao):
        get, post = self.both(origin)
        self.assertEqual(get.status, 200, origin)
        self.assertEqual(post.status, 204, origin)
        for reply in (get, post):
            self.assertEqual(reply.headers.get(ACAO), acao, origin)
            if acao is not None:
                self.assertEqual(reply.headers.get("Vary"), "Origin", origin)

    # -- allowed

    def test_a_request_with_no_origin_is_served_and_gets_no_acao(self):
        """curl, the hooks, the status line command, the notifier. A non-browser client
        needs no ACAO and must not be handed one."""
        self.assertServed(None, None)

    def test_each_spelling_of_this_crabd_is_served_and_echoed_exactly(self):
        """Three names for one address. A browser sends whichever the operator typed,
        and `[::1]` is what `localhost` resolves to first on a dual-stack machine."""
        for origin in (f"http://localhost:{self.port}",
                       f"http://127.0.0.1:{self.port}",
                       f"http://[::1]:{self.port}"):
            with self.subTest(origin=origin):
                self.assertServed(origin, origin)

    def test_the_panel_origin_is_matched_case_insensitively(self):
        """Browsers serialise an Origin lowercase, so this is belt and braces - but the
        comparison is over a scheme and a host, both of which are case-insensitive, and
        a gate that says otherwise is a gate that fails on a technicality.

        The ECHO is what was sent, not what was matched: the rule everywhere else in
        this class is that ACAO reflects the request's own Origin verbatim, and a gate
        that answered with a normalised string would be handing back an origin the
        caller never claimed."""
        origin = f"HTTP://LOCALHOST:{self.port}"
        get, post = self.both(origin)
        self.assertEqual(get.status, 200)
        self.assertEqual(post.status, 204)
        for reply in (get, post):
            self.assertEqual(reply.headers.get(ACAO), origin)
            self.assertEqual(reply.headers.get("Vary"), "Origin")

    def test_null_is_still_allowed_and_reflected(self):
        """The iCUE build's own origin. It is also forgeable, which is why the header
        gate below exists rather than this one being tightened."""
        self.assertServed("null", "null")

    def test_a_non_web_scheme_is_still_allowed_and_reflected(self):
        for origin in ("file://", "qrc://icue/widget"):
            with self.subTest(origin=origin):
                self.assertServed(origin, origin)

    # -- refused

    def test_the_right_host_on_the_wrong_port_is_refused(self):
        """The sharpest row. Any page the operator visits on 127.0.0.1 - a dev server, a
        notebook, another tool's local UI - is a DIFFERENT origin, and the allowlist is
        the bound port or nothing."""
        self.assertRefused(f"http://localhost:{self.port + 1}")

    def test_a_foreign_origin_is_refused(self):
        self.assertRefused("http://evil.example")

    def test_the_wrong_scheme_on_the_right_authority_is_refused(self):
        """https://localhost:<port> is not this origin. Nothing serves the panel over
        TLS, so a page claiming to be it is a page that is not."""
        self.assertRefused(f"https://localhost:{self.port}")

    def test_a_trailing_slash_is_not_a_valid_origin_serialisation(self):
        """No browser sends this. Anything hand-rolling an Origin header is not a
        browser obeying the same-origin policy, and a prefix match here is how an
        allowlist gets walked past."""
        self.assertRefused(f"http://localhost:{self.port}/")

    def test_a_refused_write_changed_nothing(self):
        """The point of the whole gate: a 403 that had already acked the panel would be
        a refusal in the reply only."""
        self.assertFalse(self.acked())
        self.post_ack_all("http://evil.example")
        self.assertFalse(self.acked())

    def test_an_allowed_write_really_did_the_work(self):
        """...and the negative above is only worth anything beside this."""
        self.assertFalse(self.acked())
        self.assertEqual(self.post_ack_all(self.panel_origin()).status, 204)
        self.assertTrue(self.acked())


# ------------------------------------------------------- B0: the Host allowlist

class HostAllowlistTests(PanelServed):
    """DNS rebinding, which neither of the other two gates can see.

    The attack, and why the Origin gate is blind to it: the operator visits
    `http://evil.example:<port>`, whose DNS record has a short TTL and then re-resolves
    to 127.0.0.1. The browser now believes crabd IS evil.example - so the page is
    SAME-ORIGIN with it. A same-origin GET carries no `Origin` header at all, and a
    same-origin POST carries `Origin: http://evil.example:<port>`, which the browser
    considers its own. The origin allowlist refuses the POST; nothing refuses the GET,
    and the GET is what reads `/v1/state`.

    The `Host` header is the one thing the browser sends that still names where the page
    THINKS it is, because it is taken from the URL rather than from the socket. crabd
    never read it. It does now, ahead of every other gate: this is not "who is asking",
    it is "you are not talking to who you think you are", and that is answered before
    anything about CORS.
    """

    TARGETS = ("/v1/state", "/scripts/sidecrab.js")

    def probe(self, host):
        """-> the replies for a read, a static read and a write, all carrying `host`."""
        head = {"Host": host}
        replies = [self.client.get(path, headers=dict(head)) for path in self.TARGETS]
        post_head = {"Content-Type": JSON, HEADER: "1", "Host": host}
        replies.append(self.client.request("POST", "/v1/action",
                                           body=b'{"action":"ack-all"}',
                                           headers=post_head))
        return replies

    def assertAllowed(self, host):
        read, static, write = self.probe(host)
        self.assertEqual(read.status, 200, host)
        self.assertEqual(static.status, 200, host)
        self.assertEqual(write.status, 204, host)

    def assertRefused(self, host):
        for reply in self.probe(host):
            self.assertEqual(reply.status, 403, host)
            self.assertEqual(reply.body, crabd.HOST_NOT_ALLOWED, host)
            self.assertIsNone(reply.headers.get(ACAO), host)

    # -- allowed

    def test_each_loopback_name_with_the_bound_port(self):
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                self.assertAllowed(host)

    def test_the_host_is_matched_case_insensitively(self):
        self.assertAllowed(f"LOCALHOST:{self.port}")

    def test_a_host_with_no_port_is_allowed(self):
        """`Host: 127.0.0.1` is what the hooks' curl composes when the URL carries the
        default port, and what a hand-rolled request sends. The port half of the rule
        only applies when there IS a port half."""
        self.assertAllowed("localhost")

    def test_the_hooks_own_curl_shape_passes(self):
        """The regression that would matter most: every hook POSTs through
        `curl ... http://127.0.0.1:<port>/v1/hook`, and curl composes exactly this."""
        reply = self.client.post(
            "/v1/hook",
            json.dumps({"session_id": self.SID, "hook_event_name": "Stop"}).encode(),
            headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(reply.status, 204)

    def test_an_absent_host_is_allowed(self):
        """HTTP/1.0 has no Host header, and a hand-rolled probe often omits it. Absent
        is not a claim about where the caller thinks it is, so there is nothing to
        refuse - and refusing would break `Test-SideCrab` and every raw-socket
        diagnostic in this repo."""
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(conn.close)
        conn.sendall(b"GET /v1/health HTTP/1.0\r\n\r\n")
        answer = b""
        while b"\r\n\r\n" not in answer:
            block = conn.recv(4096)
            if not block:
                break
            answer += block
        self.assertIn(b"200", answer.split(b"\r\n")[0])

    # -- refused

    def test_a_rebound_hostname_is_refused(self):
        """The attack itself: the socket really is 127.0.0.1, and the page still says
        evil.example."""
        self.assertRefused(f"evil.example:{self.port}")

    def test_a_rebound_hostname_without_a_port_is_refused(self):
        self.assertRefused("evil.example")

    def test_a_loopback_name_on_the_wrong_port_is_refused(self):
        """The port is part of the claim. A page served on another local port and
        rebound would otherwise pass on the name alone."""
        self.assertRefused(f"localhost:{self.port + 1}")

    def test_a_trailing_dot_is_a_different_name(self):
        """`localhost.` is the fully-qualified form and resolves the same, which is
        exactly why it is a bypass: it is not the string on the allowlist."""
        self.assertRefused(f"localhost.:{self.port}")

    def test_a_host_it_cannot_parse_is_refused(self):
        """A Host crabd cannot READ is a Host it cannot CHECK, and the whole gate rests
        on the header being the one thing that names where the page thinks it is.

        The two IPv6 shapes are the ones that matter: guessing which colon in
        `::1:9999` separates the port, or reading past an unclosed `[`, is exactly how a
        parser gets walked past - and a parser that answered "allowed" on a shape it did
        not understand would hand a rebound page every read. `:9999` is the third: an
        empty host is not one of the three names, whichever way it is read.
        """
        for host in ("::1:9999", f"[::1:{self.port}", ":9999"):
            with self.subTest(host=host):
                self.assertRefused(host)

    # -- order, and the things a refusal must still do

    def test_the_host_gate_answers_before_the_origin_gate(self):
        """Both would refuse this. The Host answer is the true one - the caller is not
        talking to who it thinks it is - and it is the one that has to be reachable
        when the Origin gate would have said something less specific."""
        reply = self.client.request(
            "POST", "/v1/action", body=b'{"action":"ack-all"}',
            headers={"Content-Type": JSON, HEADER: "1",
                     "Host": f"evil.example:{self.port}",
                     "Origin": "http://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.body, crabd.HOST_NOT_ALLOWED)

    def test_a_refused_write_changed_nothing(self):
        self.assertFalse(self.acked())
        self.probe("evil.example")
        self.assertFalse(self.acked())

    def test_a_refused_host_still_records_the_origin_it_carried(self):
        """ORIGIN-REC is a DIAGNOSTIC and it never decides anything, so it belongs above
        every gate - including this one, which is the newest and the first to run.

        The reading it exists for is exactly this request: a rebound page's Origin is the
        one an operator would want to see in `/v1/health` after something went wrong, and
        a recorder that runs below the Host gate records every origin except the ones
        worth reading about.
        """
        self.client.get("/v1/state", headers={"Host": f"evil.example:{self.port}",
                                              "Origin": "http://evil.example"})
        self.client.request("POST", "/v1/action", body=b'{"action":"ack-all"}',
                            headers={"Content-Type": JSON, HEADER: "1",
                                     "Host": f"evil.example:{self.port}",
                                     "Origin": "http://evil.write.example"})
        seen = {row["origin"] for row in self.client.get("/v1/health").json()["originsSeen"]}
        self.assertIn("http://evil.example", seen)
        self.assertIn("http://evil.write.example", seen)

    def test_a_preflight_is_gated_too(self):
        """Otherwise a rebound page could still learn which origins and headers crabd
        accepts, which is the map of both other gates."""
        reply = self.client.request("OPTIONS", "/v1/action",
                                    headers={"Host": f"evil.example:{self.port}",
                                             "Origin": "null"})
        self.assertEqual(reply.status, 403)
        self.assertIsNone(reply.headers.get(ACAO))
        self.assertIsNone(reply.headers.get(ACAH))

    def test_a_refused_post_does_not_desync_the_connection(self):
        conn = KeepAliveClient(self.port)._connect()
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/action",
                     body=json.dumps({"action": "ack-all", "pad": "x" * 4096}).encode(),
                     headers={"Content-Type": JSON, HEADER: "1",
                              "Host": f"evil.example:{self.port}"})
        first = conn.getresponse()
        self.assertEqual(first.status, 403)
        self.assertEqual(first.read(), crabd.HOST_NOT_ALLOWED)
        conn.request("GET", "/v1/health")
        second = conn.getresponse()
        self.assertEqual(second.status, 200)
        self.assertTrue(json.loads(second.read())["ok"])


# --------------------------------------------------------- C: the panel header

class PanelHeaderGateTests(PanelServed):
    """Every POST carries `X-SideCrab-Panel`, or it is refused.

    What it buys, and it is not "authentication": the header makes the request
    NON-SIMPLE, so a browser must preflight it, and do_OPTIONS only unlocks the header
    for an origin the allowlist already trusts. A forged-`null` page can still send
    `Origin: null` - it cannot obtain permission to send this header - so the write half
    of the forged-null residual closes here while `null` READS stay allowed for the iCUE
    build.
    """

    def test_a_post_without_the_header_is_refused_with_its_own_body(self):
        reply = self.post_ack_all(header=None)
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.body, crabd.PANEL_HEADER_REQUIRED)
        self.assertNotEqual(reply.body, crabd.CROSS_SITE_REFUSED)
        self.assertFalse(self.acked())

    def test_the_same_post_with_the_header_lands(self):
        reply = self.post_ack_all(header="1")
        self.assertEqual(reply.status, 204)
        self.assertTrue(self.acked())

    def test_every_post_path_requires_it_including_the_unknown_ones(self):
        """Not just /v1/action. The hooks, the status line command, the notifier's ack
        handler and the panel all send it, so a path that did not require it would be
        the way back in.

        /v1/metrics and /v1/logs are in this list too, and nothing in this repo
        configures the OTLP exporter's headers - an exporter pointed at crabd is refused
        until the operator sets OTEL_EXPORTER_OTLP_HEADERS. That is the contract's to
        say (v0.31.0 §4), not this test's to soften."""
        for path in sorted(crabd.Handler.MUTATING_PATHS) + ["/v1/nope"]:
            with self.subTest(path=path):
                started = time.time()
                reply = self.client.request("POST", path, body=b"{}",
                                            headers={"Content-Type": JSON}, timeout=10)
                self.assertEqual(reply.status, 403, path)
                self.assertEqual(reply.body, crabd.PANEL_HEADER_REQUIRED, path)
                # /v1/hook/permission long-polls for up to 55 s once it is INSIDE the
                # handler. The gate runs in front of the routing, so a refusal is
                # immediate - a 403 that arrived after the hold would be a hook that
                # blocked a live session for a minute. 2 s, not 10: a loopback refusal
                # is sub-millisecond, and a bound loose enough to pass a half-second
                # stall is not measuring immediacy.
                self.assertLess(time.time() - started, 2, path)

    def test_the_origin_gate_answers_first(self):
        """Order matters for what the refusal SAYS. A cross-site page learns that it is
        cross-site, which it already knew; it never learns there is a header to find."""
        reply = self.post_ack_all("http://evil.example", header=None)
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.body, crabd.CROSS_SITE_REFUSED)

    def test_a_read_never_needs_it(self):
        """GET is not the CSRF shape, the panel's own poll sends no custom header on it,
        and a read gate here would break every curl the docs tell an operator to run."""
        self.assertEqual(self.get_state().status, 200)
        self.assertEqual(self.client.get("/v1/health").status, 200)

    def test_an_empty_value_is_not_a_header(self):
        self.assertEqual(self.post_ack_all(header="").status, 403)
        self.assertEqual(self.post_ack_all(header="   ").status, 403)

    def test_the_value_is_never_interpreted(self):
        """Presence is the whole test. A value the daemon judged would be a secret, and
        a secret in a request header that a preflight can unlock is not one."""
        for value in ("1", "yes", "true", "0", "false"):
            with self.subTest(value=value):
                self.assertEqual(self.post_ack_all(header=value).status, 204)

    def test_a_refused_post_does_not_desync_the_connection(self):
        """ASSERTED ON A BARE CONNECTION. The refusal drains the body first; a body left
        in the socket is parsed as the NEXT request line, and the harness's reconnect
        would hide that. Same shape as UnknownPathFramingTests, same reason."""
        conn = KeepAliveClient(self.port)._connect()
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/action",
                     body=json.dumps({"action": "ack-all", "pad": "x" * 4096}).encode(),
                     headers={"Content-Type": JSON})
        first = conn.getresponse()
        self.assertEqual(first.status, 403)
        self.assertEqual(first.read(), crabd.PANEL_HEADER_REQUIRED)
        self.assertFalse(first.will_close,
                         "a 403 must not have to close the connection to stay in sync")
        conn.request("GET", "/v1/health")
        second = conn.getresponse()
        self.assertEqual(second.status, 200)
        self.assertTrue(json.loads(second.read())["ok"])

    def test_a_same_origin_panel_can_read_its_own_refusal(self):
        """The 403 carries the ACAO the origin gate already computed. Without it the
        panel's fetch rejects on a CORS error instead of a status, and a widget that
        cannot read the answer cannot roll its optimistic tap back."""
        reply = self.post_ack_all(self.panel_origin(), header=None)
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.headers.get(ACAO), self.panel_origin())


# ------------------------------------------------------------- C: the preflight

class PanelPreflightTests(PanelServed):
    """do_OPTIONS is where the header gate is actually enforced against a browser.

    The rule with the whole security argument in it: `Access-Control-Allow-Headers`
    lists `X-SideCrab-Panel` for a panel origin and for a non-web scheme, and NEVER for
    `null`. A forged-null iframe's preflight therefore comes back without permission to
    send the header, so its POST never leaves the browser.
    """

    def options(self, origin=None):
        headers = {} if origin is None else {"Origin": origin}
        return self.client.request("OPTIONS", "/v1/action", headers=headers)

    def test_a_panel_origin_may_unlock_the_header(self):
        reply = self.options(self.panel_origin())
        self.assertEqual(reply.headers.get(ACAO), self.panel_origin())
        self.assertEqual(reply.headers.get(ACAH), f"Content-Type, {HEADER}")
        self.assertEqual(reply.headers.get("Access-Control-Allow-Methods"),
                         "GET, POST, OPTIONS")
        self.assertEqual(reply.headers.get("Vary"), "Origin")

    def test_a_non_web_scheme_may_unlock_the_header(self):
        """file:// and qrc:// are a locally-served page that the browser did not
        collapse to an opaque origin. Not the visited-page vector, and not forgeable
        from one."""
        for origin in ("file://", "qrc://icue/widget"):
            with self.subTest(origin=origin):
                reply = self.options(origin)
                self.assertEqual(reply.headers.get(ACAO), origin)
                self.assertEqual(reply.headers.get(ACAH), f"Content-Type, {HEADER}")

    def test_null_may_never_unlock_the_header(self):
        """THE ONE THAT CLOSES THE FORGED-NULL WRITE. `null` keeps its ACAO so the iCUE
        build's reads still work, and gets exactly Content-Type back - so a sandboxed
        iframe that forges the origin still cannot obtain permission to send the header
        its POST would need."""
        reply = self.options("null")
        self.assertEqual(reply.headers.get(ACAO), "null")
        self.assertEqual(reply.headers.get(ACAH), "Content-Type")

    def test_a_cross_site_preflight_gets_nothing_at_all(self):
        for origin in ("http://evil.example", f"http://localhost:{self.port + 1}"):
            with self.subTest(origin=origin):
                reply = self.options(origin)
                self.assertIsNone(reply.headers.get(ACAO), origin)
                self.assertIsNone(reply.headers.get(ACAH), origin)

    def test_a_preflight_with_no_origin_is_answered_bare(self):
        reply = self.options()
        self.assertEqual(reply.status, 204)
        self.assertIsNone(reply.headers.get(ACAO))
        self.assertIsNone(reply.headers.get(ACAH))

    def test_no_preflight_answer_carries_a_wildcard(self):
        for origin in (None, "null", "file://", self.panel_origin(),
                       "http://evil.example"):
            with self.subTest(origin=origin):
                reply = self.options(origin)
                self.assertNotEqual(reply.headers.get(ACAO), "*")
                self.assertNotEqual(reply.headers.get(ACAH), "*")


# ------------------------------------------------------- the health diagnostic

class PanelHealthTests(PanelServed):
    """/v1/health gains a `panel` block. DIAGNOSTIC, not the contract: a support
    question is "which origins does your crabd trust", and the answer used to be
    "read the source"."""

    def health(self):
        return self.client.get("/v1/health").json()

    def test_health_names_the_origins_this_crabd_trusts(self):
        self.assertEqual(sorted(self.health()["panel"]["origins"]),
                         sorted(crabd.Handler._panel_origins(self.port)))

    def test_health_says_the_header_is_required(self):
        self.assertIs(self.health()["panel"]["headerRequired"], True)

    def test_health_names_the_directory_the_panel_is_served_from(self):
        self.assertEqual(self.health()["panel"]["dir"], str(crabd.PANEL_DIR))

    def test_the_panel_block_is_never_in_the_state_document(self):
        """Health is diagnostics; /v1/state is the contract. A key that appears in both
        is a key two documents have to agree about forever."""
        self.assertNotIn("panel", self.client.get("/v1/state").json())


# --------------------------------------------------------- D: serving the panel

class PanelTree(PanelServed):
    """A crabd whose PANEL_DIR is a temp tree, so the served bytes are known exactly.

    The tree mirrors the real one: index.html at the root, the four served directories,
    and - deliberately - three files that EXIST and must still be 404, because "it is in
    the folder" is not the rule.
    """

    def setUp(self):
        super().setUp()
        self.panel = Path(self._tmp.name) / "panel"
        (self.panel / "styles").mkdir(parents=True)
        (self.panel / "scripts").mkdir()
        (self.panel / "resources").mkdir()
        (self.panel / "mock").mkdir()
        (self.panel / "tests").mkdir()
        self.write("index.html", "<!DOCTYPE html><html><body>panel</body></html>")
        self.write("styles/sidecrab.css", "body { color: red }")
        self.write("scripts/sidecrab.js", "const POLL_MS = 3000;")
        self.write("mock/mock-state-normal.json", '{"schema":5}')
        self.write("resources/icon.svg", "<svg/>")
        # Present, and never served.
        self.write("DEV.md", "measured evidence")
        self.write("manifest.json", '{"version":"0.29.0"}')
        self.write("translation.json", '{"en":{}}')
        self.write("tests/test_ordering.js", "// harness")
        original = crabd.PANEL_DIR
        crabd.PANEL_DIR = self.panel
        self.addCleanup(lambda: setattr(crabd, "PANEL_DIR", original))

    def write(self, rel, text):
        path = self.panel / rel
        path.write_text(text, encoding="utf-8")
        return path


class PanelRoutesTests(PanelTree):
    """What is served, and what is in the folder but is not."""

    def test_the_root_serves_the_panel_index(self):
        reply = self.client.get("/")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, (self.panel / "index.html").read_bytes())

    def test_index_html_by_name_serves_the_same_bytes(self):
        self.assertEqual(self.client.get("/index.html").body,
                         (self.panel / "index.html").read_bytes())

    def test_each_served_directory_hands_back_its_file(self):
        for rel in ("styles/sidecrab.css", "scripts/sidecrab.js",
                    "mock/mock-state-normal.json", "resources/icon.svg"):
            with self.subTest(rel=rel):
                reply = self.client.get("/" + rel)
                self.assertEqual(reply.status, 200, rel)
                self.assertEqual(reply.body, (self.panel / rel).read_bytes(), rel)

    def test_a_query_string_never_reaches_the_path(self):
        """`?mock=normal` is how the panel is previewed. It selects a fixture in the
        page, and it is not part of the file name."""
        reply = self.client.get("/index.html?mock=normal&flag=dense")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, (self.panel / "index.html").read_bytes())

    def test_files_that_exist_outside_the_served_directories_are_404(self):
        """"It is in the folder" is not the rule. DEV.md is 240 kB of internal
        measurement notes, the manifest and the translations are iCUE packaging, and
        tests/ is a harness - none of them is the panel."""
        for path in ("/DEV.md", "/manifest.json", "/translation.json",
                     "/tests/test_ordering.js"):
            with self.subTest(path=path):
                reply = self.client.get(path)
                self.assertEqual(reply.status, 404, path)
                self.assertEqual(json.loads(reply.body), {"error": "not found"}, path)

    def test_an_unknown_api_path_is_still_the_json_404(self):
        """The static routes sit strictly BELOW /v1/*, so nothing about them can turn a
        mistyped endpoint into a page."""
        for path in ("/v1/nope", "/v1/state/extra", "/v1"):
            with self.subTest(path=path):
                reply = self.client.get(path)
                self.assertEqual(reply.status, 404, path)
                self.assertEqual(json.loads(reply.body), {"error": "not found"}, path)

    def test_a_missing_file_inside_a_served_directory_is_404(self):
        self.assertEqual(self.client.get("/scripts/nothing.js").status, 404)

    def test_a_directory_is_never_listed_or_served(self):
        for path in ("/styles", "/styles/", "/resources"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status, 404, path)


class PanelPathSafetyTests(PanelTree):
    """One case per test, because each is a different way in.

    The rule: percent-decode ONCE, refuse the shapes below outright, then RESOLVE the
    candidate and require the resolved panel directory to be one of its parents. The
    second half is what catches a symlink - a name that passes every text check and
    still points out of the tree.
    """

    def setUp(self):
        super().setUp()
        self.secret = Path(self._tmp.name) / "outside.txt"
        self.secret.write_text("not yours", encoding="utf-8")

    def assertRefused(self, path, leak=b"not yours"):
        """404, none of `leak` in the reply, and crabd still answering.

        The third assertion is not decoration: a refusal that raised out of the handler
        would also produce no bytes, and "it did not serve the file" is not the same
        claim as "it answered 404 and stayed up".
        """
        reply = self.client.get(path)
        self.assertEqual(reply.status, 404, path)
        self.assertNotIn(leak, reply.body, path)
        self.assertEqual(self.client.get("/v1/health").status, 200, path)

    def test_a_parent_segment_from_the_root(self):
        self.assertRefused("/../../etc/passwd")

    def test_a_parent_segment_inside_a_served_directory(self):
        self.assertRefused("/styles/../../x")

    def test_percent_encoded_parent_segments(self):
        self.assertRefused("/%2e%2e/%2e%2e/etc/passwd")

    def test_a_parent_segment_hidden_in_an_encoded_separator(self):
        self.assertRefused("/styles/..%2f..%2fx")

    def test_a_nul_byte(self):
        self.assertRefused("/styles/%00.css")

    @unittest.skipIf(sys.platform == "win32",
                     "a filename containing a backslash cannot exist on Windows")
    def test_a_backslash_separator(self):
        """Windows accepts `\\` as a separator and POSIX does not, so a path that is
        harmless on the host it was tested on is a traversal on the other.

        Both files below are REAL, because a refusal of a name that does not exist
        proves nothing. `styles\\..\\x` at the panel root is what the traversal is
        aiming at; `styles/a\\b.css` is the one only THIS rule can refuse - it sits
        inside a served directory and has no dot-leading segment, so the roots
        allowlist, the dot rule and containment all wave it through.
        """
        self.write("styles\\..\\x", "traversal target")
        self.write("styles/a\\b.css", "backslash in a real name")
        self.assertRefused("/styles\\..\\x", leak=b"traversal target")
        self.assertRefused("/styles/a\\b.css", leak=b"backslash in a real name")

    def test_a_dot_directory(self):
        self.assertRefused("/.git/config")

    def test_an_interior_empty_segment(self):
        """`/styles//sidecrab.css` names a file that IS there: strip the `//` and it is
        the stylesheet three tests above serve happily, so the empty-segment rule is the
        only thing refusing it.

        A LEADING `//` never reaches this branch and is not what this pins - and the
        mechanism is not urlsplit's. `BaseHTTPRequestHandler.parse_request` collapses a
        leading `//` to a single `/` before do_GET runs at all (CPython gh-87389, against
        open redirection: a client reads `//path` as an authority-relative URI), so the
        path _panel_target sees never has one.
        """
        self.assertRefused("/styles//sidecrab.css", leak=b"body { color: red }")

    def test_a_current_directory_segment(self):
        self.assertRefused("/styles/./sidecrab.css")

    def test_a_percent_that_survives_decoding(self):
        """A REAL file with a per-cent in its name, so the rule is the only refusal:
        `unquote` leaves `%.c` alone (it is not a valid escape) and the resolved
        candidate is a file inside the tree."""
        self.write("styles/50%.css", "a real file with a percent in it")
        self.assertRefused("/styles/50%.css", leak=b"a real file with a percent in it")

    def test_an_encoded_parent_climbing_back_into_the_panel_root(self):
        """THE DECODE-ORDER TEST, and the only case here that no second layer catches.

        `/mock/%2e%2e/DEV.md` passes the roots allowlist (its first segment really is
        `mock`) and RESOLVES to a file that really is inside the panel directory, so
        containment passes too. The single thing that refuses it is decoding BEFORE the
        segment rules run: a server that checked the raw path and then decoded would
        hand over DEV.md - and `manifest.json` and `translation.json` beside it.
        """
        self.assertRefused("/mock/%2e%2e/DEV.md", leak=b"measured evidence")
        self.assertRefused("/scripts/%2e%2e/manifest.json", leak=b'"version"')

    def test_a_symlink_inside_the_tree_that_points_outside_it(self):
        """Every text rule above passes this one. `Path.resolve()` is what refuses it."""
        link = self.panel / "styles" / "escape.css"
        link.symlink_to(self.secret)
        self.assertTrue(link.exists())
        self.assertRefused("/styles/escape.css")

    def test_a_symlink_that_stays_inside_the_tree_is_served(self):
        """The negative that makes the positive above mean something: the rule is where
        the target IS, not that a symlink was involved."""
        link = self.panel / "styles" / "alias.css"
        link.symlink_to(self.panel / "styles" / "sidecrab.css")
        self.assertEqual(self.client.get("/styles/alias.css").status, 200)


class MalformedRequestTargetTests(PanelServed):
    """A request line crabd cannot parse is a 404, never a traceback.

    `GET http://[::1/ HTTP/1.1` is a legal request LINE - absolute-form is what a proxy
    sends and BaseHTTPRequestHandler accepts it - carrying an authority urlsplit refuses
    ("Invalid IPv6 URL"). The split ran outside do_GET's try, so the ValueError walked
    out of the handler into socketserver's handle_error and printed a traceback for a
    request a scanner sends by accident.

    ASSERTED ON A BARE CONNECTION, because http.client will not compose this for us.
    """

    def raw_get(self, target: str) -> bytes:
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(conn.close)
        conn.sendall(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                     f"Connection: close\r\n\r\n".encode())
        answer = b""
        while True:
            block = conn.recv(4096)
            if not block:
                break
            answer += block
        return answer

    def test_an_unparseable_request_target_is_answered_404(self):
        answer = self.raw_get("http://[::1/")
        self.assertIn(b"404", answer.split(b"\r\n")[0])
        self.assertIn(b'{"error":"not found"}', answer)

    def test_the_daemon_is_untouched_by_it(self):
        """The half that matters: the connection it arrived on is finished with, and
        everything else is still being served."""
        self.raw_get("http://[::1/")
        self.assertEqual(self.client.get("/v1/health").status, 200)
        self.assertEqual(self.client.get("/v1/state").status, 200)

    def test_a_valueerror_from_a_handler_is_not_disguised_as_that_404(self):
        """The catch belongs to the SPLIT, not to the routed body underneath it.

        Wrapped around the whole of do_GET's routing, `except ValueError` answers
        `{"error":"not found"}` for a ValueError raised anywhere in any reader - so a
        real bug in one of them reads exactly like a mistyped path, in the daemon whose
        forbidden failure mode is silence. Narrowed, it stays what it is: it reaches
        socketserver's handle_error and prints, which is the loud answer.

        The traceback is captured rather than allowed onto the suite's own stderr; the
        assertion is that it HAPPENED, which is the whole point.
        """
        original = crabd.Handler._do_panel_log_read

        def boom(_self):
            raise ValueError("a reader that went wrong")

        crabd.Handler._do_panel_log_read = boom
        self.addCleanup(lambda: setattr(crabd.Handler, "_do_panel_log_read", original))
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            answer = self.raw_get("/v1/panel-log")
            noise = sys.stderr.getvalue()
        finally:
            sys.stderr = saved
        self.assertNotIn(b'{"error":"not found"}', answer)
        self.assertIn("ValueError: a reader that went wrong", noise)
        # ...and the daemon is still serving everything else.
        self.assertEqual(self.client.get("/v1/health").status, 200)


class PanelContentTypeTests(PanelTree):
    """The suffix decides, and an unknown one is a download rather than a guess."""

    CASES = (("index.html", "text/html; charset=utf-8"),
             ("styles/a.css", "text/css; charset=utf-8"),
             ("scripts/a.js", "text/javascript; charset=utf-8"),
             ("mock/a.json", "application/json"),
             ("resources/a.svg", "image/svg+xml"),
             ("resources/a.png", "image/png"),
             ("resources/a.ico", "image/x-icon"),
             ("resources/a.woff2", "font/woff2"),
             ("resources/a.txt", "text/plain; charset=utf-8"),
             ("resources/a.bin", "application/octet-stream"),
             ("resources/noextension", "application/octet-stream"))

    def test_each_suffix_gets_its_type(self):
        for rel, ctype in self.CASES:
            with self.subTest(rel=rel):
                if rel != "index.html":
                    self.write(rel, "x")
                reply = self.client.get("/" + rel)
                self.assertEqual(reply.status, 200, rel)
                self.assertEqual(reply.headers.get("Content-Type"), ctype, rel)

    def test_the_api_carries_the_same_two_headers(self):
        """nosniff is emitted UNCONDITIONALLY, like no-store beside it. A header whose
        job is "believe the Content-Type I declared" has no reason to be a property of
        one branch, and a per-branch flag is one more thing a new route can forget."""
        for path in ("/v1/state", "/v1/health"):
            with self.subTest(path=path):
                reply = self.client.get(path)
                self.assertEqual(reply.headers.get("Cache-Control"), "no-store", path)
                self.assertEqual(reply.headers.get("X-Content-Type-Options"),
                                 "nosniff", path)

    def test_every_static_reply_refuses_sniffing_and_caching(self):
        """no-store because the panel now ships WITH crabd: a script cached past an
        update is a panel running half of one version and half of another. nosniff
        because the type above is the answer, not a hint."""
        for path in ("/", "/index.html", "/styles/sidecrab.css",
                     "/scripts/sidecrab.js"):
            with self.subTest(path=path):
                reply = self.client.get(path)
                self.assertEqual(reply.headers.get("Cache-Control"), "no-store", path)
                self.assertEqual(reply.headers.get("X-Content-Type-Options"),
                                 "nosniff", path)


class PanelSizeBoundTests(PanelTree):
    """A bound on what one static reply may read into memory.

    _do_panel_file reads the whole file, which is right for a panel whose largest asset
    is a PNG - and wrong for a directory an operator can point anywhere with
    CRABD_PANEL_DIR, or drop a file into. A big enough file is a MemoryError, and
    MemoryError is not an OSError: it escapes the narrowed catch on the read, escapes
    do_GET's `except OSError`, and lands in socketserver's handle_error as a traceback,
    on a daemon that is now also short of memory.
    """

    def stderr_of(self, request):
        """Run `request` with the module's log-once state cleared and stderr captured.
        The handler runs on the server's own thread, and redirect_stderr rebinds the
        global sys.stderr that _log_once prints to, so it is captured all the same."""
        original = set(crabd._LOG_ONCE_SEEN)
        crabd._LOG_ONCE_SEEN.discard(crabd.PANEL_TOO_BIG_LOG_KEY)
        self.addCleanup(lambda: (crabd._LOG_ONCE_SEEN.clear(),
                                 crabd._LOG_ONCE_SEEN.update(original)))
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            out = request()
            return out, sys.stderr.getvalue()
        finally:
            sys.stderr = saved

    def spy_reads(self) -> list:
        """The name of every file Path.read_bytes is called on, in order.

        Patched the way PanelSaysWhyItServedNothingTests patches it, and for the same
        reason: this is the only way to see whether the read HAPPENED. The handler runs
        on the server's own thread, but a reply cannot be returned before the request is
        finished with, so the list is complete by the time an assertion reads it.
        """
        names = []
        original = Path.read_bytes

        def recording(path):
            names.append(path.name)
            return original(path)

        Path.read_bytes = recording
        self.addCleanup(lambda: setattr(Path, "read_bytes", original))
        return names

    def test_the_bound_is_a_named_constant(self):
        self.assertEqual(crabd.PANEL_MAX_BYTES, 64 * 1024 * 1024)

    def test_a_file_over_the_bound_is_refused_and_never_read(self):
        """SPARSE, via os.truncate: 65 MB of nothing, allocated instantly, and the
        assertion is that crabd never turns it into 65 MB of bytes.

        THE STAT IS ONLY WORTH ITS SYSCALL IF IT RUNS FIRST. A size checked after the
        read is a check on a MemoryError that already happened - the exact failure this
        bound exists to prevent - so the refusal is asserted against the reads
        themselves, not against the reply. The stylesheet at the end is what makes that
        negative mean anything: the spy does see an ordinary served file.
        """
        huge = self.panel / "resources" / "huge.bin"
        huge.write_bytes(b"")
        os.truncate(huge, crabd.PANEL_MAX_BYTES + 1)
        self.assertEqual(huge.stat().st_size, crabd.PANEL_MAX_BYTES + 1)
        reads = self.spy_reads()
        reply, noise = self.stderr_of(lambda: self.client.get("/resources/huge.bin"))
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.body, crabd.NOT_FOUND)
        self.assertIn("too big", noise)
        self.assertIn("huge.bin", noise)
        self.assertNotIn("huge.bin", reads)
        self.assertEqual(self.client.get("/styles/sidecrab.css").status, 200)
        self.assertIn("sidecrab.css", reads)

    def test_the_reason_is_logged_once_not_per_request(self):
        huge = self.panel / "resources" / "huge.bin"
        huge.write_bytes(b"")
        os.truncate(huge, crabd.PANEL_MAX_BYTES + 1)

        def three():
            for _ in range(3):
                self.client.get("/resources/huge.bin")
            return None

        _, noise = self.stderr_of(three)
        self.assertEqual(noise.count("too big"), 1, noise)

    def test_a_file_at_the_bound_is_still_served(self):
        """The boundary, without writing 64 MB: the constant is a module global read per
        request, so the test can move it rather than the file."""
        body = b"x" * 4096
        self.write("resources/exact.bin", body.decode())
        original = crabd.PANEL_MAX_BYTES
        crabd.PANEL_MAX_BYTES = len(body)
        self.addCleanup(lambda: setattr(crabd, "PANEL_MAX_BYTES", original))
        reply = self.client.get("/resources/exact.bin")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, body)


class PanelSaysWhyItServedNothingTests(PanelTree):
    """Silence is the forbidden failure mode, and a 404 is a silent answer.

    Two ways a perfectly healthy crabd serves 404 for a file that is right there, and
    both look identical from the browser - and identical to a routing bug in this
    daemon, which is where anybody would go looking first.
    """

    def stderr_of(self, key, request):
        original = set(crabd._LOG_ONCE_SEEN)
        crabd._LOG_ONCE_SEEN.discard(key)
        self.addCleanup(lambda: (crabd._LOG_ONCE_SEEN.clear(),
                                 crabd._LOG_ONCE_SEEN.update(original)))
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            out = request()
            return out, sys.stderr.getvalue()
        finally:
            sys.stderr = saved

    def test_a_file_that_cannot_be_read_is_404_and_says_so_once(self):
        """Permissions, an antivirus lock, a file being replaced under the daemon.
        Path.read_bytes is patched rather than the mode bits, because chmod 000 is a
        no-op for root and meaningless on Windows - and the branch is the same one."""
        original = Path.read_bytes

        def refusing(self):
            if self.name == "sidecrab.css":
                raise PermissionError(13, "Permission denied")
            return original(self)

        Path.read_bytes = refusing
        self.addCleanup(lambda: setattr(Path, "read_bytes", original))
        (reply, noise) = self.stderr_of(
            crabd.PANEL_READ_LOG_KEY,
            lambda: self.client.get("/styles/sidecrab.css"))
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.body, crabd.NOT_FOUND)
        self.assertIn("PermissionError", noise)
        self.assertEqual(noise.count("could not be read"), 1, noise)

    def test_a_panel_directory_that_is_not_there_says_so_once(self):
        """The one that would waste an afternoon. Point CRABD_PANEL_DIR at a typo and
        every asset is a 404 - the API answers perfectly, the page is blank, and nothing
        anywhere says the directory is the problem."""
        crabd.PANEL_DIR = self.panel.parent / "no-such-panel"

        def three():
            return [self.client.get(path).status
                    for path in ("/", "/styles/sidecrab.css", "/scripts/sidecrab.js")]

        statuses, noise = self.stderr_of(crabd.PANEL_DIR_LOG_KEY, three)
        self.assertEqual(statuses, [404, 404, 404])
        self.assertIn("no-such-panel", noise)
        self.assertEqual(noise.count("is not there"), 1, noise)

    def test_an_ordinary_missing_file_says_nothing(self):
        """The bound on the line above: a 404 for a file that simply is not in a panel
        that IS there is the ordinary answer, and logging it would be a line per
        favicon probe."""
        _, noise = self.stderr_of(crabd.PANEL_DIR_LOG_KEY,
                                  lambda: self.client.get("/scripts/nope.js"))
        self.assertEqual(noise, "")


class PanelOriginGateTests(PanelTree):
    """The static routes are gated exactly like the API - they are reads of the same
    daemon, and the panel's own script is not something a visited page may fetch."""

    def test_a_foreign_origin_cannot_fetch_the_panel_script(self):
        reply = self.client.get("/scripts/sidecrab.js",
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.body, crabd.CROSS_SITE_REFUSED)

    def test_a_plain_navigation_sends_no_origin_and_is_served(self):
        """What a browser actually does when the operator opens the page: a top-level
        navigation carries no Origin at all."""
        reply = self.client.get("/")
        self.assertEqual(reply.status, 200)
        self.assertIsNone(reply.headers.get(ACAO))

    def test_the_panels_own_origin_may_fetch_its_own_files(self):
        reply = self.client.get("/scripts/sidecrab.js",
                                headers={"Origin": self.panel_origin()})
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers.get(ACAO), self.panel_origin())


class PanelNeverBlocksTheFeedTests(PanelTree):
    """A static read touches no lock the state build holds, and holds none itself.

    Both halves matter and they are different failures. If the read took the builder's
    lock, a wedged build would stop the panel loading at all - the operator would see a
    dead browser tab and no way to find out why. If it HELD anything, a large file would
    stall the hooks, and a hook that gets no answer is a session that waits for one.
    """

    def get_on_a_thread(self, path, out, key, timeout=10):
        def run():
            started = time.time()
            client = KeepAliveClient(self.port, timeout=timeout)
            try:
                reply = client.get(path, timeout=timeout)
                out[key] = (reply.status, time.time() - started, len(reply.body))
            except Exception as exc:            # noqa: BLE001 - recorded, not swallowed
                out[key] = ("error", repr(exc), 0)
            finally:
                client.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_a_wedged_builder_does_not_stop_the_panel_loading(self):
        out = {}
        with self.builder._lock:
            thread = self.get_on_a_thread("/", out, "index")
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the static read waited on the builder lock")
        self.assertEqual(out["index"][0], 200)
        self.assertLess(out["index"][1], 2.0, out["index"])

    def test_a_large_file_does_not_stall_the_state_feed_or_a_hook(self):
        """24 MB, which is larger than anything the panel ships, requested beside ten
        polls and a hook. Every /v1/state keeps its own 2 s budget."""
        big = self.panel / "resources" / "big.bin"
        big.write_bytes(b"\xa5" * (24 * 1024 * 1024))
        out = {}
        threads = [self.get_on_a_thread("/resources/big.bin", out, "big", timeout=30)]
        for i in range(10):
            threads.append(self.get_on_a_thread("/v1/state", out, f"state{i}"))
        hook = {}

        def post_hook():
            client = KeepAliveClient(self.port, timeout=10)
            try:
                hook["status"] = client.post(
                    "/v1/hook",
                    json.dumps({"session_id": self.SID,
                                "hook_event_name": "Stop"}).encode()).status
            finally:
                client.close()

        hook_thread = threading.Thread(target=post_hook, daemon=True)
        hook_thread.start()
        for thread in threads:
            thread.join(timeout=40)
        hook_thread.join(timeout=20)
        self.assertEqual(out["big"][0], 200, out["big"])
        self.assertEqual(out["big"][2], 24 * 1024 * 1024)
        for i in range(10):
            status, elapsed, _ = out[f"state{i}"]
            self.assertEqual(status, 200, out[f"state{i}"])
            self.assertLess(elapsed, 2.0, out[f"state{i}"])
        self.assertEqual(hook.get("status"), 204)


class TheRealPanelTreeTests(PanelServed):
    """PANEL_DIR at its DEFAULT, serving the tree that actually ships.

    Everything above runs against a temp tree, which proves the routing and proves
    nothing about whether the default points anywhere real. This is the test that fails
    if the panel is moved, renamed, or the default is written relative to the process's
    working directory instead of the module's own location.
    """

    def test_the_default_directory_is_the_widget_tree_beside_crabd(self):
        self.assertEqual(crabd.PANEL_DIR,
                         Path(crabd.__file__).resolve().parent.parent / "widget")

    def test_the_root_serves_the_real_index(self):
        reply = self.client.get("/")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, (crabd.PANEL_DIR / "index.html").read_bytes())
        self.assertEqual(reply.headers.get("Content-Type"), "text/html; charset=utf-8")

    def test_the_real_panel_script_is_served(self):
        reply = self.client.get("/scripts/sidecrab.js")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body,
                         (crabd.PANEL_DIR / "scripts" / "sidecrab.js").read_bytes())
        self.assertEqual(reply.headers.get("Content-Type"),
                         "text/javascript; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
