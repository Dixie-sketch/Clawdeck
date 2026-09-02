---
title: "QA Audit — Notifier + Protocol Handlers (lane 3)"
audit: QA-Audit-2026-08-27
lane: 3 (notifier / handlers)
scope:
  - notifier/sidecrab_toast.py
  - notifier/sidecrab_ack_handler.pyw
  - notifier/sidecrab_snooze_handler.pyw
  - notifier/README.md
method: read-only, evidence-based; VERIFIED = read off the code / proven by an existing test, INFERRED = reasoned (no Windows runtime available in this lane)
date: 2026-08-27
---

# Notifier + protocol-handlers audit — lane 3

## Headline: toast-XML injection verdict

**ESCAPED — YES. No action-injection hole.** Every untrusted string the toast carries
(session title, question/body, tool name, permission summary) is rendered as **XML element
text** through `xml.sax.saxutils.escape`, which converts `&`, `<`, `>` to entities
(`sidecrab_toast.py:1736-1737`, `:1745`). Because these values land in element *content* and
never in an attribute, an attacker cannot introduce a new element or attribute or a `]]>`
break: the `<` and `>` needed to open `<action …/>` are turned into `&lt;`/`&gt;` first.

- A title of `"/><action activationType='protocol' content='pwn' arguments='calc:'/>` renders
  as `&quot;/&gt;&lt;action …` — inert. No extra `<action>`, no smuggled protocol URI into a
  button. VERIFIED by `notifier/tests/test_decider.py:387` (`test_xml_escapes_hostile_text`,
  asserts `A &amp; B &lt;tag&gt;` present and `<script>` absent) and
  `notifier/tests/test_approval.py:184` (`test_a_hostile_summary_cannot_escape_the_xml`,
  summary `</text><script>x</script>` → no `<script>` in output).
- The base64/`-EncodedCommand` path **neutralizes, it does not relocate** the injection. The
  built XML is base64'd into a single-quoted PS literal `'{xml_b64}'` whose only alphabet is
  `[A-Za-z0-9+/=]` (`:1784`, `:1794`), then the whole script is UTF-16LE-base64'd through
  `-EncodedCommand` (`:1808`, `:1812`). No quote, brace or backtick from a question can reach
  PowerShell source. VERIFIED (constants + fixed alphabet).
- The only attribute-valued interpolations carry non-attacker or regex-restricted data:
  the icon `src` is a deployer-controlled path, additionally percent-encoded (`:1744`); the
  button `arguments` are `scheme:<sessionId>` where `<sessionId>` already passed
  `^[A-Za-z0-9-]{1,64}$` (`ack_uri`/`snooze_uri`, `:1551`, `:1575`); button `content` is a
  constant. So the one saxutils weakness (its default `escape` does **not** escape `'`/`"`)
  is not reachable with attacker data. See F3 (defense-in-depth).

## Findings (ranked)

*Status 2026-08-27: F1 fixed (0.16.0, control-char strip + un-resolve). F3 + F4 fixed
(0.17.0: quote-safe attribute escaping routing-tested; case-insensitive scheme with re.ASCII).
F2 accepted-known, F5 by-design.*

### F1 — MEDIUM — A control character in a title/question/summary permanently suppresses that toast (VERIFIED gap, INFERRED failure mechanism)
`xml_escape` escapes only `&<>`; it does **not** strip XML-1.0-illegal control characters
(0x00–0x08, 0x0B, 0x0C, 0x0E–0x1F). `trim()` (`:344`) collapses whitespace via
`str.split()`, which removes 0x0B/0x0C/0x1C–0x1F but **leaves 0x00–0x08 and 0x0E–0x1B intact**.
Such a byte therefore survives into `build_xml`, into the UTF-8→base64→`LoadXml` path, and
`Windows.Data.Xml.Dom.XmlDocument.LoadXml` rejects illegal XML characters. Under the script's
`$ErrorActionPreference='Stop'` (`:1788`) that is a terminating error → non-zero return →
`show()` returns `False` (`:1822-1824`).

- **Trigger:** a `needs_input` session whose `title` or `question` — or a `pendingPermission`
  whose `summary`/`tool` — contains e.g. `\x07`, `\x08`, `\x1b`, or `\x00`.
- **Consequence, and why it is worse than a one-off dropped toast:** in
  `ToastDecider.evaluate` the spell is marked resolved (`self._resolve(sid, since)`, `:515`)
  **before** the request is built and shown, and a failed `show()` does **not** un-resolve it
  (`_emit` only logs, `:1949-1950`). So the `(sessionId, stateSince)` spell is consumed and
  **never re-toasts** until `stateSince` moves. One poisoned title = that waiting question is
  silently never surfaced by toast. The approval variant is the security-relevant one: a
  control byte in a tool `summary` suppresses the "Claude needs permission" toast (the panel
  still shows it, but the toast is the out-of-view alert). VERIFIED: escaping gap + the
  resolve-before-show ordering. INFERRED (high confidence, no Win runtime here): that `LoadXml`
  throws on the illegal char — standard XML-1.0 conformance.
- **Untested:** the ack handler rejects `\x00` *in the URI* (`test_ack_handler.py:117`), but no
  test drives a control char through `build_xml`/the toast-text path.
- **Evidence a fix would need:** either strip/replace `[\x00-\x08\x0B\x0C\x0E-\x1F]` in `trim()`
  (belongs to lane 2's shared untrusted-string handling too), or have `show()` treat a render
  failure as *not* consuming the spell so it retries. Prescription is out of scope per §3.4.1 —
  flagging the evidence.

### F2 — LOW (known/accepted) — Lost snooze in the notifier/handler read-modify-write window
`toast-state.json` is written by two processes with no lock: the notifier
(`write_state_section`, `:961`) and the snooze handler (`write_snooze`→`apply_snooze`,
`snooze_handler:189/116`). Both do read-modify-write of the whole document onto a PID-suffixed
temp + `os.replace`. **No torn/corrupt file is possible** (atomic replace, per-PID temps
prevent interleave — VERIFIED). But a snooze written after the notifier reads and before it
`os.replace`s is overwritten and lost.
- **Consequence:** the waiting toast re-fires up to 30 min early. Never a corrupt file, never a
  wrong ack. Bounded small: the notifier writes ~twice/day + a stamp every 15 min, so the
  collision window is tiny.
- **Status:** already disclosed in `README.md` Open-risks ("Two processes write
  toast-state.json …") and reasoned correctly. Logging for completeness, not as a new defect.

### F3 — LOW (defense-in-depth) — Attribute values rely on `saxutils.escape`, which does not escape quotes
`build_xml` escapes attribute values (icon `src`, button `content`/`arguments`) with the same
`xml_escape` that leaves `'`/`"` untouched (`:1745`, `:1771-1774`). Today every such value is a
constant, a regex-`^[A-Za-z0-9-]{1,64}$` id, or a percent-encoded path, so **no reachable
path** carries a raw quote into an attribute (VERIFIED — this is why F-headline is a clean
YES). It is nonetheless a latent trap: any future edit that puts less-restricted data into an
attribute would open real attribute-breakout, because the escaping there is quote-blind.
`test_snooze.py:181` (`test_a_quote_that_would_escape_the_attribute_is_refused`) guards the
*current* id path only. Evidence for hardening: `quoteattr()` or `escape(v, {'"':'&quot;',
"'":'&#39;'})` on attribute interpolations.

### F4 — INFO — Case-sensitive scheme match can silently drop an ack/snooze
`parse_ack_uri`/`parse_snooze_uri` require `text.startswith("sidecrab-ack:")` **case-sensitively**
(`ack_handler:87`, `snooze_handler:107`), but Windows URL schemes are matched
case-insensitively, so the shell may hand back `SIDECRAB-ACK:…`. That would be refused
(EXIT_BAD_URI, length-only log). Deliberate per the code comment ("a case difference is a bug
to see in the log"), not a security hole — a robustness note only. No action.

### F5 — INFO — HKCU registration is same-user-hijackable, by design
The two schemes and the `SideCrab.Notifier` AUMID live under `HKCU\SOFTWARE\Classes`
(`ack_handler:8`, `snooze_handler:7`, `sidecrab_toast.py:1583`). Any process running as the
same user can repoint `sidecrab-ack\shell\open\command` at malware or re-register the AUMID —
but same-user code already owns the session and could POST to the unauthenticated crabd API
directly, so this grants no new privilege. Cross-user is blocked (per-SID hive). Surfaced
explicitly for lane 5's whole-trust-chain review; no notifier-side change indicated.

## Audited SOUND (checked, no defect)

- **Toast action-injection via title/question/tool/summary** — closed by element-text escaping;
  base64 + `-EncodedCommand` closes the PowerShell layer independently. (F-headline; VERIFIED)
- **Handler URI validation — completeness.** `^[A-Za-z0-9-]{1,64}$` is fully anchored (`^…$`),
  applied after strip + one-trailing-slash tolerance (`ack_handler:75-93`,
  `snooze_handler:95-113`). Charset admits no `/ . : % " '` or whitespace → no path traversal
  (`../../windows/system32` refused — VERIFIED `README:370`, `test_ack_handler.py`), no scheme
  smuggling, no percent-escape. Interior newline / trailing junk rejected by the `$` anchor.
  Only `argv[0]` is consumed; extra args are ignored. VERIFIED.
- **What a valid-charset id can do is tightly bounded.** A crafted-but-valid id (e.g. `---`)
  causes exactly one thing: `post_ack` POSTs `{"sessionId":id,"action":"ack"}` to localhost
  crabd (`ack_handler:123-137`), which 404s for a nonexistent id. Nothing beyond an ack for
  that id; the id can never become a URL/path/scheme. VERIFIED.
- **Ledger integrity — no corruption.** Atomic `os.replace`, per-PID temp names, read-modify-
  write preserving other keys, and every read/parse wrapped so a corrupt/half-written file
  reads as empty (`read_state_doc:951`, `parse_snooze_map:1050`, `write_snooze:205-209`). A
  crash mid-write leaves the old file intact (worst case an orphaned `.tmp.<pid>`). VERIFIED.
  (Lost-update window is F2.)
- **Snooze cannot ack.** `sidecrab_snooze_handler.pyw` imports no `urllib`/`http`/`socket` and
  only writes the `snooze` key; it never contacts crabd (VERIFIED by inspection; the README
  notes an AST test pins this). The asymmetry (snooze = notification, ack = question) holds.
- **An approval can never be snoozed.** Approval requests are built `actionable=False` so they
  carry no snooze button (`build_approval_request:618`), and `ApprovalDecider.evaluate` is not
  passed the snooze map at all (`poll_once:1981` vs the waiting decider at `:1980`). A snooze
  mark for a session id cannot suppress its approval toast. VERIFIED — matches the README table.
- **Snooze defers, ack wins.** Snooze leaves the spell unresolved so it re-fires once after
  expiry (`:501-513`); an `acked` flag resolves it permanently even mid-snooze (`:487-490`).
  VERIFIED, and mutation-covered ("snooze marks the spell instead of deferring it | 4").
- **Snooze-map is re-validated on read-back.** `parse_snooze_map` re-applies the session-id
  charset and drops unparseable instants (`:1063-1071`) — a hand-edited/hostile ledger key
  cannot inject, and neither "snoozed forever" nor "snoozed until now" is fabricated. VERIFIED.
- **AUMID probe trust.** Positive answer latches, negative re-probes on a cooldown
  (`registered_aumid:1641`); injected probes are never process-cached (`:1652`). The
  borrowed-vs-registered distinction is logged with the actual `winerror`. No trust defect.
- **Handlers are silent-by-construction and best-effort-logging.** No traceback can reach a
  user's screen (`__main__` guards, `ack_handler:167`, `snooze_handler:250`); `log_line` never
  raises (`:101/167`); a refused URI logs only its length, never the unvalidated string
  (`ack_handler:150`, `snooze_handler:235`). VERIFIED.

## One-line recheck ask for the verify wave
Confirm F1's mechanism on a real box: fire `build_xml` for a `ToastRequest(title="x\x07y", …)`
through the actual PowerShell 5.1 path and confirm `show()` returns `False` (LoadXml throws),
then confirm the spell is not retried on the next poll. Everything else here is read off the
code or an existing test.
